# WP7 第一次验收（opus，2026-10-14）：REJECTED，返工清单

验收对象：wp7/send-links @ 7b27a7044，base ef2fb79ac。10 条验收标准：1、2、3、4、5、7、8 PASS，9 PASS（两处偏差记录），6 字面不符（两处 docstring 命中 grep），10 待 PR 时核。产品行为、边界、门禁、回归均通过；驳回只因测试与证据。

## 不符项（全部必修，不改产品代码除非测试暴露错误）

1. 规格 §7 评审 R3 要求的测试缺失且未声明 BLOCKED。补齐：
   a. 只读副作用守卫：用 spy 断言写 notice 与查询 openRequestsFrom 时，不调用 ack、reply、容量释放接口，不喂控制器样本，不改 W/cwnd。D1 现只断言「原请求仍 pending、新消息是 request」，补 spy。
   b. 回收排干：真 inbox 先 ack 排干，再处理旧的 turn.completed，断言不生成 notice，openRequestsFrom 不出现。
   c. owner 的请求同样被提醒（不因没有 orgfs link 文件而过滤）。
   d. 四种 LINK_* hold 下 send 返回字段保真（ok、accepted、queued、holdReason 不变）：若无法在本分支测（WP2/WP4 未合），在证据里明确写 BLOCKED-on-WP2/WP4 并给出将来的测试名与断言。
2. 规格 §5 要求的 UNKNOWN/BLOCKED 清单落进 docs/evidence/org-structure-constrain-2026-10-14/wp7/（新文件 blocked.txt 或追加到 green.txt）：W1 的 pending 投影级断言 BLOCKED-on-WP3-A；真实 interactive 的 turn 上报 BLOCKED-on-WP5；1d 若 BLOCKED 也列入。
3. gates.txt 对 §4.6 第二条 grep（`grep -rn "hyprial.biz" src/hyprial/daemon`）记录不如实：实际 2 行 docstring 命中（application/ports.py:3 为 base 既有；wiring/reply_reminder_hooks.py:6 为本包新增）。改写 reply_reminder_hooks.py 的 docstring 使 grep 不命中，并在 gates.txt 如实记录命令输出。

## 建议（采纳的在证据里写明）
- consumer.py 的 `_completed` 在 finally 里无条件清 started_at_ms 并回写 last_end_ms：若旧 completed 事件在下一个 turn.started 之后被重放，会清掉新 turn 的起点。改为只在非重放（window_key 与本事件不同）时推进状态，并加测试。
- 删掉 `open_requests_by_sender` 的死参数 now_ms，或真的传下去（二选一并加测试）。
- 补建议 A2 的边界用例：fetched 恰等于 end、晚于 end、recipient 不同、I1 与 I2 同 id 去重。
- D4 加一句断言确认 X 的 inbox 收到了消息。

## 交给 WP5/WP8（记录，不在本包做）
- WP5：interactive 载体上报 turn.completed 时 actor 用 canonical URI、endedAtMs 为真实时刻、deliveryId 允许空串；同时上报 turn.started。
- WP8：contract/ 同步 message.send 可选字段 openRequestsFrom；真实载体 summary-only 注入与 hold/W 联测。
- WP2：observe_delivery 仍用 _has_route，daemon 级 lane 收不到 delivery.*（WP2 已按裁定 46 不走 bus）。
