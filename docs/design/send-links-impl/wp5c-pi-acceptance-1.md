# WP5-C pi attach 第一次验收（opus，2026-10-15）：REJECTED

分支 wp5c-pi/send-links，HEAD d8caa3abb，基线 ba6ae30dc（WP5-A）。worktree /private/tmp/hf-wp5c-pi-send-links，git status 干净；提交 48c4e7694（红）→ 3e139b225（实现）→ d8caa3abb（证据 + 小改 TS）。全部命令经 uv run 使用 worktree 自带 .venv。

结论：硬切、注入、结算、提示文字、闸门全部达标；但 turn 报告重试队列有一个丢失唤醒的竞态（B7 核心机制），已实测导致新测试 P9 约 10% 偶发失败，且实现未按 wp5c §2.3「poll 道每轮尝试队首」接入 poll 道。返工量小（见文末清单），修好后可直接复验。

## 逐项

1. 硬切 PASS。§7.3 第一条 grep 为空；G3p/G4p 通过。逐行读 TS：全文件唯一的写调用是 message.settle_hint、message.ack、session.turn.started/ended、session.register*/refresh/heartbeat/unregister、message.pending.list/wait；无 message.reply、无 submit、无失败文本。parsePending 改名 row 后只读 messageId/from/to/intent/kind/summary/bodyBytes/origin，不读 row.message；rewake 重注入路径也经 parsePending。三元拆成两行只是让按行计数为 2，语义上确实只有 2 个 turn 方法字面量，不是规避。
2. 注入 PASS。injectionBlock 只渲染 summary/intent/kind/messageId/bodyBytes/读正文提示，发送者与收件人在头里（senderSuffix）。P2 读 tests/daemon/harnesses/carrier/injection_golden.json，与 Python K2（test_carrier_contract.py:31）为同一文件，非复制；6 例全等。小差异（不阻断）：行缺 intent 时 TS 填 "event"、Python contract.injection_from_row 填 ""；golden 不覆盖此情形。
3. turn 报告 FAIL。正确部分：一个 turn 一个 reportId（openTurnIfNeeded/endTurn）；队首未确认不发后续；started 被接受后才做 fetched 读（P5 时间差 ≥0.28s）；shutdown 先 ended(failed) 再 unregister（P8）；SUPERSEDED/STALE 清队列；扩展无任何持久化（无 fs 写），重启后新 sessionRef，旧队列随进程消失，不会重发（裁定 64 PASS，P10 语义正确）。缺陷：sendHeadReport 失败时仅当 reportTimer 为空才排定时器；若另一调用方（agent_settled/agent_start/injectQueued 的 flushReports）恰在 retryAt 到点、定时器尚未触发时发送并失败，reportRetryAt 被推后而定时器仍是旧的；旧定时器触发后 flushReports 因 Date.now() < reportRetryAt 直接 return，不再排定时器。poll 道不调用 flushReports，于是队列停住，直到下一次 agent 事件或新消息注入。空闲会话的 ended 可能无限期不报，WP7 的窗口迟迟不闭合。
4. 结算 PASS。轮末对每个注入 id 恰一次 settle_hint；ack→message.ack（MESSAGE_ACK_UNAVAILABLE 视为已结算）；keep→无帧并入 kept；任一调用失败保留 outstanding，由 poll 道（settled 标志 + completing 单飞）重试；kept 阻止 applyChange 再入队；rewakeReason 非空时清 kept（P7、P10 同 epoch 10 次 poll 注入 0 次）。说明：rewakeReason 除 epoch 变化外还来自 STALE_SESSION 后的重注册（"start"），这本身就是 daemon 重启自愈，视同换纪元，接受。
5. 提示文字 PASS。sessions.py pi 追加提示与 §2.5 逐字一致；文件 702 行。
6. 测试 FAIL（一项）。P1–P10、G3p、G4p、G5 共 13 条齐全；在 ba6ae30dc 的临时 worktree 中检出 48c4e7694 的 tests，13 条全红，失败原因与 §6.1 表一致（注入含正文、存在 reply 帧、无 turn 帧、源码含 "message.reply"/record.message 等）；临时 worktree 已移除。新文件连跑：前 3 次中 1 次失败，再 6 次全过，单跑 P9 20 次失败 2 次（失败点均为 P9(c) 清错误后 10s 内等不到 ended）。旧测试删除核查：test_pi_reply_settlement.py 的 8 条全部针对 message.reply 回执语义，删除正确；test_pi_harness_attach_extension.py 中 6 条回复类测试删除正确；但 test_injection_names_user_store_candidates_when_ambiguous 被删除而非改写，它守的 describeSender 的 candidateUsers 分支（TS 125–132 行）仍在，属丢失的守护。另外旧 test_one_settled_run_sends_exactly_one_reply_even_when_a_poll_overlaps 守的「agent_end 与 agent_settled 之间 poll 不得提前结算」与旧 negative-receipt 测试守的「结算瞬时失败只重试、不重注入」在新套件中无对应测试（本人探针三条均通过，行为仍正确，只是缺守护）。
7. 运行 PASS（除第 6 项的偶发）。tests/daemon/harnesses/pi + carrier（HYPRIAL_REQUIRE_PI_SDK=1）：217 passed、2 xfailed、1 failed（已知 controlled_loader t04-06）。tests/daemon 全量：6654 passed、2 failed（均为已知：pi controlled_loader t04-06、ipc heartbeat ratio），5 skipped、7 xfailed、1 xpassed。structural limits violations 为空；module boundaries violations 0；ruff 通过；git diff --check（除 evidence）干净；pi_harness_attach.ts 800 行；§7.4 计数 settle_hint 1、turn 方法 2；G5 grep 为空；禁区 diff 为空。注释删减核查：删去的注释全部描述已删除的回复/失败文本行为，deliverAs、message_start 消费证明、单飞、rewake 绕过去重等协议说明保留。小瑕疵：第 84 行 REPORT_RETRY_CAP_MS 尾注释「settle_hint "keep" is held until the epoch changes」与该常量无关，属误放。
8. 合并就绪 PASS。merge-tree 对 refactor/org-structure-constrain（现为 abc83dd1c，4c2ff4c59 是其祖先，之后只有一条 run log 文档提交）、wp5c-codex/send-links（cd4397fea）、wp5c-claude/send-links（34b193ded）均无冲突；合并树上 G5 grep 为空。
9. Contract-Impact（交 WP8）见下文。
10. 证据 FAIL（小）。wp5c §7 要求的 pi-frames.json（P3、P4、P9 帧序列）缺失；red/green/gates/blocked 齐全。
11. 真实 pi TUI BLOCKED-on-WP8（agent_start/agent_settled 实际触发、followUp 队列、真实 daemon 端到端）；pi/resources 无 TS 编译/lint 工具，脚本由 node 直接执行，记 BLOCKED，同意 blocked.txt。

## 探针（临时文件，未提交）

- 竞态定位：P9 单跑 20 次失败 2 次；在临时 worktree 中仅对 flushReports 的提前返回分支补一句「若无定时器则按 reportRetryAt 排一个」后，同一用例连跑 30 次 0 失败，确认根因是丢失唤醒而非测试时序。
- 结算瞬时失败：settle_hint 前两次 DAEMON_BUSY → 第三次成功并 ack，注入仍 1 次，无 reply。PASS。
- ack 瞬时失败：第二次 ack 成功，注入仍 1 次。PASS。
- agent_end 与 settled 之间 6 个 poll 周期：无 settle_hint、无 ended；settled 后恰 1 次 settle_hint。PASS。
- candidateUsers 发送者行：TS 仍输出「NOT verified: sender ambiguous between users g1, g2」。PASS（行为在，只缺测试）。
- 空 turn：让 fetched 读返回空，40 个 poll 周期内只产生 1 对 started/ended(completed)，0 次注入，无重复。按 wp5.md §4 与 WP7 §0.1 判断：fetched_at_ms 只写一次（projection.py COALESCE），空 turn 的窗口内不会有新戳，ReplyReminderConsumer 查询为空、不提醒；它只把 last_end 前移，不污染下一窗口；seq 每次空 turn 只多 2 个，且仅在「观察到新消息但 fetched 读前已被他处结算」的竞态下出现。接受该设计。

## 接受的偏差

- 重试耗尽时把本批 id 放入 kept（不 ack、不回复、消息仍 pending，到下一纪元才再注入），规格只写「丢弃本批」。可防止同一会话内反复注入失败批次，接受。
- 结算瞬时失败由 poll 道每轮重试，而非规格写的「下一 turn」，更及时，接受。
- agent_settled 中先 await 一次 flushReports 再结算：失败时 flushReports 立即返回，不阻塞结算，满足「结算不等待该报告确认」。
- P2 用一个测试循环 6 例，接受。

## Contract-Impact（交 WP8）

- pi 注入文本改为 wp5.md §2.3 的 summary-v1 块；pi 批量头改为「Each item below is a summary only; read a body on demand and reply or ack each item explicitly.」；单条头保持原格式。golden 在 tests/daemon/harnesses/carrier/injection_golden.json。
- pi_interactive 会话开始发送 session.turn.started（reportId）与 session.turn.ended（reportIds、status completed/failed），按 reportId 重试，进程重启丢弃未确认报告（裁定 64）。
- pi 载体在轮末对每个注入 id 调 message.settle_hint（带 sessionRef），只按 ack 执行 message.ack；pi 不再提交 message.reply，原「轮末自动回复最终助手文本」与「诚实失败回复」语义取消（影响 WG-001 autoReply 字段的解释）。
- pi 追加系统提示文字改变（sessions.py）。

## 集成者合并时要做

- 合入前确认返工后的 P9 稳定（建议单跑 ≥30 次）。
- 合入次序与 codex/claude 两条无关，merge-tree 均无冲突。
- PR 正文写 Contract-Impact 上述四点与 Skill-Impact（hyprial-ops：pi interactive 不再轮末自动回复）。

## 返工清单

1. 修 turn 报告重试的丢失唤醒：flushReports 因 reportRetryAt 未到而返回时，若无 reportTimer 则按剩余时间排定时器；或按 wp5c §2.3 在 runPollLane 每轮 void flushReports()（二者择一或都做）。加一条确定性测试复现「定时器挂起期间另一调用方发送失败」的情形（例如在 started 持续失败时于第一次重试点附近反复触发 agent_start，清错误后断言最终被接受），并单跑 P9 30 次附进 green.txt。
2. 恢复 test_injection_names_user_store_candidates_when_ambiguous，期望文本改为头 + expected_block。
3. 补两条守护：(a) agent_end 与 agent_settled 之间多个 poll 周期不发 settle_hint/ended；(b) settle_hint 或 ack 瞬时失败后由 poll 道重试，且不重注入、不 reply。
4. 补 docs/evidence/org-structure-constrain-2026-10-14/wp5c/pi/pi-frames.json（P3、P4、P9 帧序列）。
5. 删除或改正第 84 行 REPORT_RETRY_CAP_MS 的误放注释（保持 ≤800 行）。
