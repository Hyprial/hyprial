# WP5-C：三个 interactive 载体（pi attach、codex、claude channel）

- 版本：v2，2026-10-14（从 [wp5.md](wp5.md) v2 拆出；并入评审 B2、B3、B4、B7 载体侧、B9 的 R3/R4）。总览、子包顺序与禁区见 wp5.md §1。
- 分支：`wp5c/send-links`（可再分 `wp5c-pi`、`wp5c-codex`、`wp5c-claude` 三条，文件不相交，合并次序不限）。
- 前置：WP5-A 合入集成分支（`contract.py`、`inbox/contracts/settlement.py`、`session.turn.started`、按 reportId 的 turn 去重、`settle_hint` 对 reply/event 答 ack）。
- ⛔ 不改：WP5-A 的全部文件、`capabilities.py`（WP5-B）、`pi_harness_bridge.ts`（WP6）、`biz/`（含 `turn_pulse.py`）、`kernel/`、`links/`、`inbox/`。契约有缺口时停下报集成者。

## 1. 文件与行数预算（本分支实测）

| 文件 | 现 | 动作 | 预算 |
| --- | --- | --- | --- |
| `daemon/impl/harnesses/pi/resources/pi_harness_attach.ts` | 717 | §2 | ≤ 801（验收实测，裁定 75） |
| `shell/impl/cli/commands/agent/runtime/sessions.py` | 702 | 只改 pi 追加系统提示（§2.5） | 702 |
| `daemon/impl/harnesses/codex/settlement.py` | 596 | 删回复分支与 `_settle_reply_unavailable`；结算走 contract | ≤ 520 |
| `daemon/impl/harnesses/codex/carrier.py` | 677 | 注入渲染、turn 上报、`known` 并入 KEPT | ≤ 760 |
| `daemon/impl/harnesses/carrier/codex_carrier_store.py` | 296 | `KEPT` 状态、`injection_format` 列、`turn_report` 表、迁移 | ≤ 420 |
| `daemon/impl/harnesses/carrier/interactive_carrier_runtime.py` | 530 | `CarrierKept` 命令；恢复认 KEPT | ≤ 640 |
| `daemon/impl/mcp/channel/adapter.py` | 427 | turn 状态、`harness_read` 补报 start、轮末 settle | ≤587（验收实测，裁定 75） |
| `daemon/impl/mcp/channel/loops.py` | 365 | 合并 start/end 脉冲、按序转发 | ≤ 470 |
| `shell/impl/cli/commands/agent/runtime/claude.py` | 479 | `UserPromptSubmit` hook 写 start 脉冲 | ≤ 500 |

目录直接文件数不变（`harnesses/codex/` 7、`harnesses/carrier/` WP5-A 后 4、`mcp/channel/` 6、`shell/.../agent/runtime/` 7）；测试目录见 wp5.md §1.6。

## 2. pi attach（`pi_harness_attach.ts`）

### 2.1 删除

- `finalAssistantText(messages)`（154–174 行）与 `pi.on("agent_end", …)`（666–673 行）；`OutstandingBatch.finalText`。
- `settleMessage(message, text)`（476–519 行）整段，含 `isRequest ? "message.reply" : "message.ack"` 与 `CODE_REPLY_UNAVAILABLE` 探测；常量 `CODE_REPLY_UNAVAILABLE`（75 行）。
- `failOutstanding(reason)`（534–541 行）与两段 honest failure 文本。
- 文件头注释第 4 条（20–23 行）改写为契约描述。
- `parsePending` 的 `text: … record.message …`（197 行）与 `PendingMessage.text`。

### 2.2 注入与结算

- `PendingMessage` 增 `kind`、`summary: string | null`、`bodyBytes: number`；`parsePending` 只读这些键与 `messageId/from/to/intent/origin`。
- `injectionBlock(message)` 逐字实现 wp5.md §2.3；`formatBatch` 用它，批量头改为 `Each item below is a summary only; read a body on demand and reply or ack each item explicitly.`
- `completeOutstandingOnce()`：对 outstanding 中不在 `completed`、`kept` 的每条：`message.settle_hint` → `ack` 时 `message.ack` 并入 `completed`；`keep` 时入 `kept`。任一调用抛错：保留 outstanding，由 poll 道重试（沿用 `settled` 单飞标志）。`kept` 与 `completed` 一样阻止 `applyChange` 把该 id 放回 `queuedForInjection`；daemon 换 epoch（`rewake`）时清空 `kept`，允许一次重新注入。

### 2.3 turn 报告与重试（B7）

- 状态：`openTurn: string | null`；`reportQueue: Array<{phase: "started" | "ended", reportId, status?}>`，按入队顺序发送，队首未确认不发后续。
- `openTurnIfNeeded()`：`openTurn === null` 时 `openTurn = crypto.randomUUID()`，入队 started，并**等待队首 started 被 daemon 接受**后返回；失败时返回但 `openTurn` 与队列保留（不换 id）。
- `pi.on("agent_start")` 调 `openTurnIfNeeded()`；`injectQueuedOnce()` 在 `fetchPending()` 之前 `await openTurnIfNeeded()`，started 未被接受时本次不做 fetched 读（wp5.md §4.4）。
- `pi.on("agent_settled")`：有 outstanding 且未 consumed 时走现有重注入（`MAX_INJECT_RETRIES` 不变），不结束 turn；重试耗尽时 `endTurn("failed")`，丢弃本批（不 ack、不回复，消息仍 pending），`notify(..., "warning")`。其余情况 `endTurn("completed")` 后结算本批。
- `endTurn(status)`：入队 `{phase: "ended", reportId: openTurn, status}`，`openTurn = null`；结算不等待该报告确认。
- 发送队列的重试：poll 道每轮尝试队首；传输错误或 authority 超时 → 保留、退避；`SESSION_SUPERSEDED`/`STALE_SESSION` → 清空队列并 `notify` 诊断。确认后出队。
- `session_shutdown`：`openTurn !== null` 时入队 `ended(failed)`，best-effort 冲一次队列，再 unregister。
- 进程边界：扩展进程重启即重新注册、新 sessionRef，旧报告必被 fence 拒绝，因此队列只在进程内保留（wp5.md §9.5 第 3 条）。

### 2.4 无代答

整个文件不出现 `"message.reply"`（G3p）。

### 2.5 提示文字

`sessions.py:160–165` 的 pi 追加系统提示中「answer them in the transcript and the bridge sends your final reply back when the turn settles」改为「each carries only a summary; read a body with harness_read_body, and answer each with harness_reply or harness_ack — nothing is sent on your behalf」。claude 的提示（`claude.py:348`）不改。

## 3. codex interactive

### 3.1 删除

- `_settle_final` 的 `else: method = "message.reply"; …final_output…` 分支（167–171 行）与其后的 reply 专用判定（191–209 行）。
- `_settle_reply_unavailable`（251–333 行）整段、`reply_message_id` import、`worker.carrier.settlement.reply_lost`。
- `carrier.py::_apply_inbox_list` 的 `text = item.get("message")` 与相应 `continue`（543–545 行）。

### 3.2 注入与 turn 报告（B7）

- 注入：`HarnessDelivery(message=render_injection(injection_from_row(item)), …)`；store 写入时 `injection_format = INJECTION_FORMAT`。
- turn 报告持久化：`codex_carrier_store.py` 新表 `codex_turn_report(actor, session_ref, report_id, phase, status, delivery_id, created_ms, PRIMARY KEY (actor, report_id, phase))`。`_on_turn_started` 在 `record_turn_started` 后写 started 行（`reportId = turn_id`，`deliveryId = delivery.delivery_id`）；`CarrierFinalObserved` 提交后、结算前写 ended 行（`status: completed`）；`CarrierRemoved` 分支中 turn 为 failed/interrupted 时写 ended 行（`status: failed`），不调 settle_hint。
- 发送：carrier 每个 poll 周期按 `(created_ms, phase started 先于 ended)` 发送该 actor 当前 session 的未确认行，确认后删除；同一 reportId 的 ended 不先于其 started 发出。传输或 authority 失败 → 保留、退避；`SESSION_SUPERSEDED`/`STALE_SESSION` → 删除并记 `worker.carrier.error`（stage `turn-report`）。结算不等待报告确认。carrier 进程重启后从表中恢复未确认行。

### 3.3 结算与 KEPT（B2）

- `_settle_final` 改为 `settle_injected(IpcCarrierPort(self.daemon_request, self._signed), [message_id])`：`acked` → 原 `_settle_confirmed`（`CarrierSettled`，删行）；`deferred` → 原 `_defer_settlement`；`MESSAGE_ACK_UNAVAILABLE` 仍走 `_settle_ack_unavailable`。
- `kept` → 新命令 `CarrierKept(generation, delivery_id)`，由 `interactive_carrier_runtime.py` 的 carrier actor 在**一个命令内**：store 置 `KEPT`、清 `final_output`、释放运行中投影与 `_states`。⛔ 不得先由适配器写 KEPT 再发会删行的 `CarrierSettled`；`CarrierSettled` 只用于 ack 已确认的清理。命令按 generation fence，过期 generation 丢弃。
- 去重键 `(actor, message_id)`（store 主键）。`CodexCarrierStore.load` 的跨 session 集合从 `{FINAL_OBSERVED, REMOTE_SETTLED}` 扩为 `{FINAL_OBSERVED, REMOTE_SETTLED, KEPT}`；新增 `kept_ids(actor) -> frozenset[str]`（不按 session）。新 session 继承同 actor 的 KEPT。
- `_recover`：KEPT 行不生成 enqueue 或 supply-final 效果，不恢复为 FINAL_OBSERVED，只进入 `known`。
- `known`：`_apply_inbox_list` 与 `_list_may_hold` 都并入 `kept_ids(actor)`。只剩 KEPT 的 pending 行时 poll 道可进入 `message.pending.wait`，不得持续全量 list。
- 删除 KEPT 行：只在一次**成功**的权威 `message.pending.list`（`ok: true` 且 `messages` 为数组）确认该 id 不在其中时删除。传输失败、`ok: false`、数组无效、`pending.wait` 的增量结果都不算消失证据。

### 3.4 无代答

`codex/settlement.py`、`codex/carrier.py` 不出现 `"message.reply"`（G3p）。`capabilities.py:167` 的注释与能力位由 WP5-B 改。

### 3.5 升级前的 store 行（B3）

- 升级前的行 `injection_format IS NULL`。
- FETCHED（未起 turn）：先对 daemon 取该 id 的 metadata（`message.pending.list` 中的行：`summary`、`bodyBytes`、`kind`、`intent`），按 wp5.md §2.3 重建注入文本、写回 `message` 列并置 `injection_format`，再按常规注入。⛔ 不把旧 `message` 当 summary，不由旧正文派生 summary。metadata 暂不可得（传输失败）→ 保持 FETCHED、不起 turn、退避重试。权威列表成功且不含该 id → 删行（已被处理）。行在列表中但无 summary → 用无 summary 的固定提示（`summary: (none; …)`）渲染。
- TURN_STARTED：只对已运行的 turn 查状态，不重发旧文本。
- FINAL_OBSERVED：只按 settle_hint 结算，⛔ 不提交保存的最终文本。
- REMOTE_SETTLED：只做本地清理（现行为）。

## 4. claude channel（B4）

### 4.1 信号

- `claude.py` 的会话 hook 配置增加 `UserPromptSubmit`：`python -m hyprial.biz.impl.turn_pulse --signal-dir <turn_signal_dir>/start`（模块不改，timeout 5）。`SessionStart`/compact 不算模型 turn，不写 start 脉冲。
- 子目录 `<turn_signal_dir>/start/` 与 `<turn_signal_dir>/state/` 不被现有 `_pending_turn_signals` 的 `is_file()` 扫描收录；无新 CLI 选项。

### 4.2 adapter 与 loops

- turn 状态：`open_turn: str | None` 持久在 `<turn_signal_dir>/state/open-turn`（原子写）；`turn_injected: set[str]` 只在进程内。
- `loops._forward_turn_signals` 每轮收集 start 脉冲与 end 脉冲，按 `(st_mtime_ns, start 先于 end, 文件名)` 排序后逐个处理：
  - start：`open-turn` 文件记 `{reportId, origin: "pulse" | "read"}`。无 open turn → `open_turn = 脉冲名`、origin `pulse`，`session.turn.started(reportId=open_turn)`。open turn 的 origin 为 `read`（由 `harness_read` 补报）→ 这是同一 turn 的迟到 start：origin 改为 `pulse`，删脉冲，不另报。open turn 的 origin 为 `pulse` → 上一 turn 没有 Stop（例如 StopFailure）：先报 `ended(reportIds=[open_turn], status: failed)`，确认后再按「无 open turn」开新 turn。
  - end：有 open turn → `session.turn.ended(reportIds=[open_turn])`；确认后删脉冲与 `open-turn`，再对 `turn_injected` 中每个 id 按 `settle_injected` 结算（ack/keep，异常留到下轮），清空集合。无 open turn → 现行为 `reportIds=[脉冲名]`（WP7 规则 2）。
  - 任一报告未确认：脉冲与 `open-turn` 留在磁盘，下轮同 reportId 重试（现有语义扩到 start）；`SESSION_SUPERSEDED` 时安静退出（现行为）。
- `ClaudeChannelAdapter.harness_read`：若 `open_turn` 为 None，先**同步**生成 uuid 作 `open_turn`（origin `read`，写 `open-turn`）并完成 `session.turn.started` 的确认，再做 `fetched` 读；started 未确认时返回错误给工具调用，不做 fetched 读。之后到达的同一 turn 的 start 脉冲按上条吸收。
- `harness_read` 返回的 `messages` 中 `kind != "notice"` 的 messageId 加入 `turn_injected`。门铃观察（`_remember_messages(observed=False)`）与 `pending.wait` 不计为注入。
- 已显式 reply/ack 的 id 轮末再问：settle_hint 答 ack、ack 得 `MESSAGE_ACK_UNAVAILABLE`，按已结算处理（幂等）。

## 5. keep 的恢复（B9 的 R3、R4）

- R3（pi）：同 epoch 反复 poll 不重复注入 kept；仅 daemon epoch 改变时按 P7 允许一次 rewake 注入。扩展进程重启后：pending 保留在 daemon，kept 集合与未确认 turn 报告丢失，新会话按普通 pending 注入一次并由 WP7 提醒。P10 把这一行为作为明确记录的边界测出来，不冒充 headless 的零再派发保证。
- R4（codex）：X8 的三个断点与 X9 的 KEPT-only 等待、失败 list 不删去重记录。

## 6. 测试清单

运行与先红规则同 wp5.md §7。pi 测试沿用 `_pi_attach_support.py` 的 `SDK_AVAILABLE`；CI 设 `HYPRIAL_REQUIRE_PI_SDK=1`，缺 SDK 报 BLOCKED。

### 6.1 pi：`tests/daemon/harnesses/pi/extension/test_pi_carrier_contract.py`

`FakeDaemon` 增加 `message.settle_hint`（按设定答 ack/keep）、`session.turn.started`、`session.turn.ended`（可设定「接受但丢响应」「拒绝」）；pending 行带 `kind/summary/bodyBytes` 与 `message: "BODY-SENTINEL"`。

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| P1 | `test_injection_is_summary_only` | 注入含 summary、`bodyBytes: 13`、messageId，不含 `BODY-SENTINEL` | `AssertionError`：注入含正文 |
| P2 | `test_injection_matches_the_python_golden` | golden `row` 驱动单条注入 == 头 + `\n` + expected | 同 P1 |
| P3 | `test_settled_run_asks_settle_hint_and_never_replies` | 每个注入 id 恰一次 settle_hint；ack 得 `message.ack`，keep 无帧；`frames_for("message.reply") == []` | `AssertionError`：存在 reply 帧 |
| P4 | `test_turn_boundaries_are_reported_in_order` | `agent_start` 后有 started；settled 后 ended(`completed`) 帧序号早于第一个 settle_hint | `AssertionError`：无 turn 帧 |
| P5 | `test_started_is_accepted_before_the_fetched_read` | 空闲时来新消息：started 的响应早于第一个 `fetched: true` 的 pending.list 请求 | 同 P4 |
| P6 | `test_retry_exhaustion_reports_failed_and_settles_nothing` | 连续 abort 至耗尽：ended(`failed`)；无 reply、无 ack；消息仍 pending | `AssertionError`：存在 honest failure reply |
| P7 | `test_kept_message_is_not_reinjected_until_rewake` | keep 后多次 poll 不再注入；换 epoch 后注入一次 | 同 P3 |
| P8 | `test_shutdown_with_open_turn_reports_failed` | 有 open turn 时 shutdown：先 ended(`failed`) 再 unregister | `AssertionError` |
| P9 | `test_turn_report_retries_with_the_same_report_id` | (a) ended 被接受但响应丢失：重试同 reportId，假 daemon 只记 1 次新接受；(b) ended 到达前失败：下轮重试成功；(c) started 未确认时不发 ended、不换 id；(d) `SESSION_SUPERSEDED` 清空队列 | `AssertionError`：无重试 |
| P10 | `test_extension_restart_boundary_is_recorded` | R3：同 epoch 10 次 poll 注入 kept 0 次；扩展重启（新会话）后该 pending 注入恰 1 次 | `AssertionError` |
| G3p | `test_pi_attach_never_replies` | 文件无 `"message.reply"`、`finalAssistantText`、`failOutstanding`、`settleMessage`、`CODE_REPLY_UNAVAILABLE` | `AssertionError` |
| G4p | `test_pi_attach_never_reads_the_body` | 文件无 `record.message` | `AssertionError` |
| G5 | `test_no_prompt_promises_an_automatic_reply` | `src/hyprial` 下无 `sends your final reply back`、`your final reply is sent back` | `AssertionError` |

删除 `test_pi_reply_settlement.py`（8 条，全测回复结算）。改写 `test_pi_harness_attach_extension.py` 中 `test_settled_run_replies_with_final_assistant_text`、`test_one_settled_run_sends_exactly_one_reply_even_when_a_poll_overlaps`、`test_event_intent_is_acked_not_replied`、`test_reply_unavailable_is_benign_not_a_retry_storm`、`test_inject_retry_exhaustion_fails_outstanding_honestly`、`test_run_without_final_text_completes_with_honest_failure` 与断言注入含正文的 `test_injection_names_*` 两条。

### 6.2 codex：`tests/daemon/harnesses/codex/test_codex_carrier_contract.py`

沿用 `test_codex_carrier_settlement.py` 的组装与假 `daemon_request`。

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| X1 | `test_codex_prompt_is_summary_only` | `HarnessDelivery.message == render_injection(...)`；store 行不含 `BODY-SENTINEL`，`injection_format == "summary-v1"` | `AssertionError` |
| X2 | `test_codex_final_settles_by_hint_and_never_replies` | FINAL_OBSERVED 后依次 `session.turn.ended`、`message.settle_hint`，ack 时 `message.ack`；从无 `message.reply` | `AssertionError`：收到 reply |
| X3 | `test_codex_keep_is_not_fetched_again` | keep：store `KEPT`、`final_output` 为空；同 id 仍在 list 中不再 fetched、不起 turn；成功 list 中消失后行被删 | `AssertionError`：行被 `CarrierSettled` 删除 |
| X4 | `test_codex_turn_start_is_reported_with_delivery_id` | started 参数 `reportId == turn_id`、`deliveryId == delivery_id` | `AssertionError` |
| X5 | `test_codex_failed_turn_reports_failed` | thread/read failed：ended(`failed`)，不调 settle_hint | `AssertionError` |
| X6 | `test_legacy_store_rows_never_inject_the_body` | 升级前 schema 建含 `BODY-SENTINEL` 的 FETCHED、TURN_STARTED、FINAL_OBSERVED 行后启动新 carrier：新注入不含 sentinel；FETCHED 在 metadata 不可得时不起 turn；所有结算无 `message.reply` | `AssertionError`：注入含 sentinel |
| X7 | `test_codex_turn_reports_survive_restart_and_retry` | ended 首次传输失败后 carrier 重启：从 `codex_turn_report` 恢复并以同 reportId 重发；接受但丢响应时只 1 次新接受；`SESSION_SUPERSEDED` 删行 | `AssertionError` |
| X8 | `test_kept_survives_three_breakpoints` | R4：写 KEPT 后 actor 清理前、carrier 进程重启、同 actor 换 sessionRef：每个断点后仍 pending、无 reply、无新 turn；显式 reply/ack 后 KEPT 被清 | `AssertionError` |
| X9 | `test_kept_only_waits_and_failed_list_keeps_dedup` | 只有 KEPT 行：poll 进入 `pending.wait`；list 传输失败、`ok:false`、数组无效、wait 增量：KEPT 行都不删 | `AssertionError`：持续全量 list |
| G3c | `test_codex_never_replies_or_reads_body` | `codex/settlement.py`、`codex/carrier.py` 无 `"message.reply"`、`_settle_reply_unavailable`、`reply_lost`、`item.get("message")` | `AssertionError` |

改写：`test_codex_carrier_settlement.py`（13 条中测 reply 的部分）、`tests/shell/cli/agents/codex/test_settlement.py`。

### 6.3 claude：`tests/daemon/channels/adapters/test_channel_carrier_contract.py`

真 `DaemonApplication` 与真 channel adapter（沿用 `tests/daemon/channels/mcp/lifecycle/` 的组装），WP7 消费者真接线。

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| L1 | `test_first_turn_of_a_new_session_gets_a_reminder` | 新会话第 1 个 turn：start 脉冲 → 真 `harness_read`（fetched）→ Stop：未回复的 r1 得到提醒；fixture 不预置 `last_end` | `AssertionError`：无提醒 |
| L2 | `test_turn_end_settles_by_hint_without_answering` | 同一 turn 读到 request、reply、event：Stop 报告确认后 reply、event 被 ack，request keep；无任何 `message.reply` 帧 | `AssertionError`：reply/event 未 ack |
| L3 | `test_late_start_signal_is_backfilled_by_read` | 无 start 脉冲时 `harness_read`：fetched 读之前已确认 started；迟到的 start 脉冲被吸收，不产生第二个 started；该 turn 提醒覆盖 r1 | `AssertionError`：fetched 早于 started |
| L4 | `test_turn_signals_are_retried_across_child_restart` | start 已报、end 报告失败后 channel 子进程重启：`open-turn` 与脉冲仍在，重试同 reportId，只 1 个 `turn.completed` | `AssertionError` |

`tests/shell/cli/agents/claude/test_hooks.py` 增 L5 `test_user_prompt_submit_writes_a_start_pulse`：生成的 settings 含 `UserPromptSubmit`，参数为 `turn_pulse --signal-dir <turns>/start`。先红：`KeyError: 'UserPromptSubmit'`。

## 7. 验收（WP5-C；可机检）

1. `PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/harnesses/pi/extension tests/daemon/harnesses/codex tests/daemon/channels tests/shell/cli/agents/claude -q` 通过；pi 部分在 `HYPRIAL_REQUIRE_PI_SDK=1` 下 skip 为 0。命名测试存在：P1–P10、G3p、G4p、G5、X1–X9、G3c、L1–L5 共 33 条（`grep -rhoE "def test_[a-z0-9_]+" <三个新文件> tests/shell/cli/agents/claude/test_hooks.py | sort -u` 含上表全部名字）。
2. wp5.md §8 第 3–5 条同样执行。
3. 以下 grep **无输出**：
   - `grep -n '"message.reply"\|finalAssistantText\|failOutstanding\|settleMessage\|record.message\|CODE_REPLY_UNAVAILABLE' src/hyprial/daemon/impl/harnesses/pi/resources/pi_harness_attach.ts`
   - `grep -n '"message.reply"\|_settle_reply_unavailable\|reply_lost\|item.get("message")' src/hyprial/daemon/impl/harnesses/codex/settlement.py src/hyprial/daemon/impl/harnesses/codex/carrier.py`
   - `grep -rn "sends your final reply back\|your final reply is sent back" src/hyprial`
   - `git diff --name-only <base>..HEAD -- src/hyprial/daemon/impl/harnesses/carrier/contract.py src/hyprial/daemon/impl/inbox src/hyprial/daemon/impl/runtime src/hyprial/daemon/impl/hooks src/hyprial/daemon/impl/application src/hyprial/daemon/impl/harnesses/capabilities.py src/hyprial/biz src/hyprial/kernel src/hyprial/daemon/impl/links`
4. 以下 grep **恰有输出**：
   - `grep -c '"message.settle_hint"' …/pi_harness_attach.ts` → `1`
   - `grep -c '"session.turn.started"\|"session.turn.ended"' …/pi_harness_attach.ts` → `2`
   - `grep -n "class CarrierKept" src/hyprial/daemon/impl/harnesses/carrier/interactive_carrier_runtime.py` → 1 行
   - `grep -n "KEPT" src/hyprial/daemon/impl/harnesses/carrier/codex_carrier_store.py` → ≥ 2 行（状态常量与 load 的跨 session 集合）
   - `grep -n "UserPromptSubmit" src/hyprial/shell/impl/cli/commands/agent/runtime/claude.py` → 1 行
   - `grep -c "session.turn.started" src/hyprial/daemon/impl/mcp/channel/adapter.py` → ≥ 1
5. `wc -l` 满足 §1；目录文件数满足 wp5.md §1.6。
6. 先红证据与 PR 正文同 wp5.md §8 第 10、11 条。真实 pi TUI、codex TUI、Claude Code 联测（含 `UserPromptSubmit` 对 channel 推送 turn 是否触发）记 BLOCKED-on-WP8。

证据：`docs/evidence/org-structure-constrain-2026-10-14/wp5c/`：`red.txt`、`green.txt`、`gates.txt`、`pi-frames.json`（P3、P4、P9 帧序列）、`blocked.txt`。
