# WP3-A 第三次验收（opus，2026-10-15）：ACCEPTED

验收对象：wp3/send-links @ 22fa43389（返工 3：61e0ad393 测试先红 → cd15f8ca6 修复 → 22fa43389 证据去尾空格）。
基线：dcc0c664f（集成者合并 WP4 后集成分支 + 返工 3 清单）；合并基 6999b6ab6。
对照：refactor/org-structure-constrain @ 9470434f6（含 a4900d08f 裁定 67），wp6/send-links @ 073435594，origin/dev @ 503348f47。
检查清单：wp3-acceptance-2.md 原 6 条 + 集成者补充返工 3 清单 5 条。
工作树 git status 验收前无已修改的跟踪文件；本次只新增本文件。

## 逐项结论（返工 3 清单）

R3-1 七条红测试改到 WP4 语义且不削弱断言：PASS。
- 在 dcc0c664f 上以其自带测试复现 7 红（test_call_paths 1、test_pending_shape 1、test_read_body 3、test_fetch_one 2），与集成者记录一致。
- 逐条对比 `git show dcc0c664f:<path>`：test_call_paths 由本节点单个 URI 扩为本节点与远端 dispatch-coordinator、远端 _turn-hooks 双向豁免，并新增近似名 `_named-worker` 双向受管，断言加强；test_fetch_one 两条只把 `message_id="x"` 改为 `only=frozenset({"x"})`，facade 一条另绑 PermissiveLinkGate（WP4 拉取在 actor 内准入，必须绑），断言逐字不变；其余 4 条只把 `app._link_gate = ...` 改为 `app.link_admission.bind(...)`，断言逐字不变。
- 重复读测试由 `gate.admitted == ["x","x"]` 改为 `["x"]` 并加 fetch receipt 计数与两次返回相等，这是返工 4 要求的行为变化，不算削弱。

R3-2 唯一 gate 槽（裁定 67）：PASS。
- `grep -rn _link_gate src/` 无结果（tests 中只剩 test_wp0_seams 的函数名 test_link_gate_is_runtime_checkable）。DaemonApplication 不再声明该属性。
- queries.py 的 `_admit_read_body` 与 `_ipc_message_settle_hint` 均取 `self.link_admission.gate`。
- test_read_body_asks_the_one_composed_gate_slot：在 dcc0c664f 代码上红，HEAD 绿。

R3-3 保留行回退泄漏与 gate 抛错：PASS（附接受的偏差 D1）。
- test_a_held_row_acked_without_admission_is_not_readable、test_an_expired_governed_row_never_admitted_is_not_readable、test_a_failing_gate_reads_fail_closed_and_logs 在 HEAD 绿。
- 草稿探针（scratch worktree，未提交）：admit=False 被拒 → message.ack → 再读，MESSAGE_BODY_UNAVAILABLE；过期受管行（expires_at_ms=2）admit=False 下 MESSAGE_BODY_UNAVAILABLE；gate.admit_dispatch 抛 RuntimeError → MESSAGE_BODY_UNAVAILABLE、日志含 link.gate_error、未盖 fetched；ack 之后 gate 再抛错，保留行分支同样 MESSAGE_BODY_UNAVAILABLE。

R3-4 重复读断言 daemon 行为：PASS。
- 测试断言两次返回相等、fetched_at_ms 不变、_CountingPublisher 计数 1、gate 调用列表 `["x"]`。`FakeLinkGate.admitted` 每次调用都追加，不去重；去重的 `reserved` 列表不再被断言。
- 实现：`_read_body_already_fetched` 用 `has_fetched(message_id)`，已盖章的 pending 行不再问 gate、不再拉取、不再发 receipt。
- 草稿探针走真实 DeliveryCustodyFacade 路径（FetchPendingCommand → fetch_pending_admitted）：第一次读 gate 被问 2 次（read_body 预判 1 次 + actor 内准入 1 次），第二次读 0 次，receipt 1 次。重复读只读成立；首读双问见 D2。

R3-5 证据与门禁：PASS。rework-3-red.txt（11 failed / 505 passed）、green-focus、daemon-shell、gates、runs.md 齐全；测试提交 61e0ad393 只含 tests/ 与证据。

## 逐项结论（二次验收原 6 条）

1. link_governed 与 WP4 一致：PASS。admission.py 取 WP4 版，`git diff 6999b6ab6 HEAD -- dispatch/admission.py links/ports.py` 为空；read_body 用 `self.link_admission.governs(...)`；docstring 已改为按保留名、双向。
2. 保留行回退泄漏：PASS（见 R3-3、D1）。
3. gate 抛错 fail-closed：PASS（见 R3-3）。
4. 重复读测试：PASS（见 R3-4）。
5. 改用 FetchPendingCommand.only：PASS。inbox 中无 `message_id=` 的 fetch 收窄；ports 只有 `only: tuple[str, ...] | None`；queries.py 调 `fetch_pending(key, only=frozenset({message_id}))`。
6. 合入集成分支并解冲突：PASS。core.py 保留三表循环 ALTER 与 WP7 system_notices.summary ALTER；retry.py 读写 summary；receive.py INSERT 含 summary 列。

## 其他核对

- settle_hint 幂等只读：PASS。探针对同一 id 连问两次结果相同，行快照前后不变；test_settle_hint 全绿。
- 裁定 56（非 request 的 pending 行答 ack）：WP3-A 未实现。探针：pending 的 reply 行在绑定答 keep 的假 gate 下返回 keep；未绑 gate 时任何 pending 行都是 keep。按 README 裁定 56 原文「合入时由集成者或 WP5 补」与工作包 5a（WP5-A 的 settle_hint_for），留给 WP5-A，接受。WP3-A 证据未写明这一点，本文件补记。
- 迁移幂等：PASS。test_the_three_tables_gain_summary_and_keep_legacy_rows 重开已迁移库不重复 ALTER。
- summary 往返：PASS。zenoh 帧（test_an_explicit_reply_to_survives_every_durable_path 中 encode/decode_delivery_frame 断言 summary）、outbox 重试、custody 三条路径均有断言。
- receive.py summary 列（集成者手工补回）：PASS。变异检验：在草稿树删掉 INSERT 中的 summary 列后，test_summary_survives_the_inbox_direct_path、outbox_retry_path、custody_path、explicit_reply_to 两参数化与 facade 探针共 6 条变红；还原后全绿。接收后的 summary 可经 read_body 与 pending.list 读出（探针：`('x', 'SUM-x', 'message')`）。

## 先红后绿

- 草稿 worktree：dcc0c664f 代码 + 61e0ad393 的 tests/，焦点命令 11 failed / 505 passed（全在 test_read_body 10 条与 test_settle_hint 1 条），与证据一致。
- HEAD 焦点命令：516 passed。

## 回归与失败分类

HEAD `tests/daemon tests/shell -q`：4 failed / 8102 passed / 6 skipped / 5 xfailed / 1 xpassed（24 分 55 秒）。
worker 报告的 26 条中，tests/shell/squire/setup 的 24 条与 pi attach wait、home lease io stage 本次全套均未复现；squire/setup 单独在 HEAD、集成分支、dev 上也全绿。判为 worker 运行环境所致（iv）。

| 测试 | HEAD | 集成 9470434f6 | dev 503348f47 | 分类 |
| --- | --- | --- | --- | --- |
| pi test_pi_controlled_loader::test_t04_t05_t06 | 红 | 红 | 红（单独） | (i) dev 既有；断言 SDK 版本 '1.2.0' == '0.85.1' |
| shell test_shell_cli_entry::test_whole_registered_command_inventory | 红 | 红 | 红（单独） | (i) dev 既有；多出 'dispatch' |
| ipc cost test_four_sides_cover_a_pure_heartbeat_load | 红（全套） | 红（全套） | 单独绿；HEAD、集成单独也绿 | (iv) 全套负载下计时比例，非代码 |
| login/device test_boundaries::test_t11_daemon_tree_never_imports_login | 红 | 红（单独与全套） | 绿（单独） | (ii) 集成分支引入：b0142b862「feat(links): pure link model, controller and admission policy」新增的 links/controller.py、model.py、policy.py 未被 resident_offenders 归类（fail-closed）。WP3-A 不新增条目 |
| shell/gui/lifecycle 5 条 | 绿 | 红（全套） | 未跑 | (iv) 集成草稿树未执行 GUI `npm ci`；HEAD 树已装 GUI 依赖 |
| shell/squire/setup 24 条（worker 报告） | 绿 | 绿（单独） | 绿（单独） | (iv) |

(iii) WP3-A 引入：0 条。(ii) 只有 t11 一条，来自集成分支 WP1 提交，不是本包的账，但集成分支合 dev 前必须修（登记 links 纯模型文件或改守卫）。

## 门禁

- check_structural_limits.py：exit 0，violations 为空。
- check_module_boundaries.py：exit 0。
- `uv run ruff check .`：All checks passed。
- `git diff --check 6999b6ab6 HEAD`：空。
- `git diff 6999b6ab6 HEAD -- src/hyprial/daemon/impl/links/ports.py src/hyprial/daemon/impl/dispatch/admission.py`：空。

## 接受的偏差

D1. 保留行（已 ack 或已过期、不再 pending）的回退分支对受管行仍调 `gate.admit_dispatch` 现问，而不是证明该行曾被准入。探针：held 行 ack 之后、或过期且从未准入的行，在 gate 改答 True 后 read_body 返回正文（pending=False）。接受理由：wp3.md 阻断 R3 的规格允许「已消费、已过期」行走只读回退；T13 要求「已 ack 的本人消息只读返回」，而 ack 本身会以 COALESCE 写 fetched_at_ms，现有持久状态区分不了「准入后 ack」与「hold 中被 ack」；gate 拒绝时不泄漏。风险留给 WP2：对已结算或已过期的 id 调 `admit_dispatch` 不得占 W（或由 WP2 的 tick 核对立即释放）。代码注释「gate 对未准入的 id 会拒绝」是对 gate 的假设，ports 文档未写，WP2 实施时须核对。
D2. 经真实 facade 首读一次 gate 被问 2 次（预判 + actor 内准入）。ports 规定 admit_dispatch 按 (recipient, message_id) 幂等，接受。测试夹具 durable_application 直接用 InboxService（不经 actor 准入），所以测试里看到 1 次。另：若两次判定之间 gate 翻成 False，actor 内不盖章但 read_body 仍返回正文并报 fetched=True；窗口极小，记录不返工。
D3. 裁定 56 留给 WP5-A（见上）。

## 合并试算

- `git merge-tree --write-tree refactor/org-structure-constrain HEAD`：无冲突，树 9deea9c02。集成分支比本分支多 a4900d08f、9470434f6 两个纯文档提交。
- `git merge-tree --write-tree wp6/send-links HEAD`：无冲突，树 51195028b。在该合并树上跑 channel/summary、inbox/summary、channels/mcp、tests/kernel：525 passed / 1 failed（kernel POSIX listener fd，dev 既有）。
- 删除放行后的验证：在该合并树上把放行集合改为空集，test_mcp_delegate_removed.py 2 passed，确认 WP3-A 已注册路由，放行可删。

## 集成者合并时待办

1. 与 WP6 同批合并（裁定 41）。删除 `tests/daemon/channels/mcp/contracts/test_mcp_delegate_removed.py` 中 `test_delegate_tool_is_not_advertised_and_surface_routes` 的放行（wp6/send-links 上第 285–293 行）：注释三行、`pending_daemon_handler = {"message.read_body"}` 与列表推导中的 `and method not in pending_daemon_handler`。
2. 集成分支合 dev 前修 t11（来自 b0142b862）。
3. WP2 规格 v2 §9.1 的 `_link_gate` 依赖按裁定 67 改读 `link_admission.gate`；WP2 核对 D1 的 W 计数风险。
4. 裁定 56 的 reply 行答 ack 由 WP5-A 实现并补 T15。

## 交给 WP8 的契约清单（本包不改 contract/）

- 新 IPC 方法 `message.read_body`：入参 messageId 加身份参数；返回 ok、messageId、conversationId、from、to、intent、summary、message、bodyBytes、origin、pending、fetched。
- 新 IPC 方法 `message.settle_hint`：返回 ok、messageId、hint ∈ {ack, keep}、pending。
- `message.send` / `message.reply` 入参 `summary`；send 的 `replyTo` 非空字符串校验（INVALID_ARGUMENT）。
- 线框 envelope 可选键 `summary`（zenoh 帧）；对 agent 目标的 reply body 新增 `replyTo` 键（digest 变化，裁定 39）。
- `message.pending.list` 行形状新增 `kind`、`summary`、`bodyBytes`（A 期保留 `message`）。
- 错误码 `MESSAGE_BODY_UNAVAILABLE`（data `{messageId}`）、`SUMMARY_REQUIRED`（data `{summaryBytes, maxBytes}`）。
- kernel `worker_channel.METHOD_PARAMS`：send/reply 放行 summary，新增两方法。
- `ipc/wire.py` 的 summary 映射归 WP3-B（裁定 40）。
