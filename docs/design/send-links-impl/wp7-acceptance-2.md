# WP7 第二次验收（opus，2026-10-14）：ACCEPTED

验收对象：wp7/send-links @ 04d892f35，base ef2fb79ac，返工自 7b27a7044。清单为 wp7-acceptance-1.md。
测试在 worktree 自带 .venv 下以 uv run 执行。新增运行记录见 docs/evidence/org-structure-constrain-2026-10-14/wp7/acceptance-2-runs.txt。

## 逐项判定

1a 只读副作用守卫 PASS：test_r3_read_only_guard_notice_and_lookup_never_touch_custody 用转发 spy 断言调用集合只含 pending_messages、fetched_between、receive_system_notice，且 inbox 行（fetched_at_ms/acked）前后不变；D1 新增 test_d1_send_never_acks_replies_or_settles_the_original_request 对 ack、settle_harness_failure、fetch_pending、retry_due 打 spy，断言无针对 A/r1 的调用。
1b 回收排干 PASS：test_r3_reclaim_drain_then_old_completed_yields_no_notice，先 ack 再喂旧 turn.completed，system_notices 为空、open_requests_by_sender 为 {}。
1c owner 请求 PASS：test_r3_owner_request_is_reminded_without_a_link_file，user:wp7-owner 的请求在 I1、I2 两条路径都返回 o1，字段与 notice 均生成。
1d LINK_* 保真 BLOCKED-on-WP2/WP4：blocked.txt 写明未来测试名 test_d5_open_requests_field_preserves_hold_result[LINK_*] 与断言。核实确属阻塞：grep -rn admit_send src 仅命中 links/ports.py:143，send 路径无 gate 注入点，本分支产不出任何 hold。
2 BLOCKED/UNKNOWN 清单 PASS：blocked.txt 含 W1 投影 BLOCKED-on-WP3-A（grep bodyBytes src/hyprial/daemon 无 pending.list 投影）、interactive turn 上报 BLOCKED-on-WP5、1d BLOCKED-on-WP2/WP4、R3 分区副本、UNKNOWN 项。
3 grep 如实 PASS：grep -rn "hyprial.biz" src/hyprial/daemon 仅输出 application/ports.py:3（base 既有），与 gates.txt 返工段一致。
建议 _completed 重放 PASS：每 actor 记 (source, seq) 高水位，旧事件记 reply_reminder.stale_replay 并返回，finally 只在非重放时推进。红绿已实测：39927b151 的测试对 0865f3459 代码 6 failed（2 个 stale replay 断言失败，4 个 now_ms TypeError），HEAD 43 passed。
建议 now_ms PASS：reply_reminders.py 签名与 send.py 调用均已删除，红绿同上。
建议 A2 边界 PASS：test_a2_window_edges_recipient_and_id_dedup（等于 end 计入、晚于 end 不计、他人 recipient 不计、I1/I2 去重）与 test_a2_first_fetch_stamp_is_not_rewritten。
建议 D4 PASS：test_d4 断言 X 的 inbox 有 1 条 intent=request。
R2 同事件重试 PASS：既有 test_b6_* 三条通过；另以临时探针验证下一 turn.started 之后的同事件重试仍会补发（2 次 post）。
回归 PASS：uv run --extra test pytest tests/biz tests/daemon -n auto，2 failed / 6535 passed / 5 skipped；两条失败均在 full-suite.txt 既有清单内，与 WP7 无关。
门禁 PASS：check_structural_limits.py 与 check_module_boundaries.py 均 "violations": []；uv run ruff check . 输出 All checks passed!；git diff --check ef2fb79ac HEAD 无输出；links/ports.py 未改动。

## 接受的偏差
- R3 守卫测试在修复前的「红」只来自 now_ms 签名错误，守卫断言本身到达即绿；作为回归护栏接受。
- 1a 中「不喂控制器样本、不改 W/cwnd」：本分支无控制器，spy 只能覆盖 inbox 层方法；接受，联测交 WP8。
- 1b 以 open_requests_by_sender 返回 {} 代替经 message.send 断言字段缺席；二者同一读路径，接受。

## 发现（不阻塞，交 WP5）
高水位在同一 source 的 seq 回到 0 时会让该 actor 的 turn.completed 全部被判为 stale，直到 consumer 重建或该 actor 被 LRU 淘汰（临时探针：seq=50 之后 seq=0、1 均 0 次 post）。本分支不可达：turn.* 唯一来源是 DaemonHookService（source daemon.turns，计数器 _sequence），consumer 由 wire_reply_reminder_hooks 在同一对象上构建，两者同生同灭。严重度：低（潜在）。WP5 若允许载体自带 source/seq 上报 turn，必须保留 daemon 统一盖 seq，或在 consumer 中按 source 纪元（epoch）重置高水位，并加测试。
