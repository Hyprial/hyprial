# WP3-A 第二次验收（opus，2026-10-14）：REJECTED

验收对象：wp3/send-links @ c5c5e981f（返工 2addf6bf6 测试先红 → c5c5e981f 修复；b0b65c418 并入集成分支 7c24bf7ec）。
对照：wp4/send-links @ 8c720faf6，集成分支 refactor/org-structure-constrain @ 0ec2bd8fe，wp2/send-links @ 86342ef0a（仅规格）。
运行记录：docs/evidence/org-structure-constrain-2026-10-14/wp3a-acceptance-2-runs.txt。

## 逐项结论

B1 link_governed 与 WP4 一致：FAIL。
- 本分支 `if sender in internal_actors:`；WP4 8c720faf6 为 `if sender in internal_actors or recipient in internal_actors:`（裁定 51，本分支基线 7c24bf7ec 已含该裁定）。探针：`link_governed("agent:op:node-1:w", "agent:op:node-1:dispatch-coordinator", internal_actors={coordinator})` 返回 True，裁定 51 要求 False。
- `_admit_read_body` 的 docstring 写「both directions」，代码只豁免 sender，文实不符。
- 已修部分 PASS：本节点 dispatch-coordinator 转发在无 gate 时可读（test_a_daemon_internal_forward_is_readable_without_a_gate 先红后绿）。
- 远端节点 `agent:op:node-2:dispatch-coordinator` 发来的消息无 gate 时仍 MESSAGE_BODY_UNAVAILABLE。裁定 45/51 要求按保留名、不限节点；WP4 composition 同样只放本节点 URI，此项按裁定 55 归 WP4 二次验收，不单独记 WP3-A 的账，但合并后 read_body 必须跟随 WP4 的判定。
- 逐字比对：函数签名、docstring、parse_agent_uri 判定相同，仅上述一行（及 WP4 的 3 行注释）不同；文件结构另有差异（见合并冲突）。

B2-R2 两种回复形态保留原 id：PASS。test_an_explicit_reply_to_survives_every_durable_path（outbox/custody/线框/inbox）、test_a_send_with_reply_to_saves_the_original_id_and_is_not_acked、test_a_plain_send_in_the_same_conversation_is_not_a_reply、test_a_refused_reply_send_does_not_ack_the_original 均绿。

B2-R3 gate 准入：FAIL。
- 已覆盖：admit=False 不可读且状态不变、未绑定 fail-closed 并记 link.gate_unbound、False→True 后同 id 可读、owner 与自发不受拒、回复的 intent/reply_to 原样交给 gate。
- 未做到「held 行不经 retained-row 回退泄漏」：探针 1，admit=False 被拒后接收方 message.ack，再 read_body 返回正文 `SECRET`；探针 2，过期（expires_at_ms=2）的受管行在 admit=False 下 read_body 返回正文、pending=False，gate 一次都没被问。原因：`queries.py` 中非 pending 分支 `harness_failure_original` 回退不做 `_admit_read_body`，也不要求该行曾被准入或 fetched。违反 wp3.md「对尚未准入的受管消息……返回 MESSAGE_BODY_UNAVAILABLE」与 §2.6 第 3 步（已被 TTL 清掉 → 同码）。
- test_repeated_reads_reserve_one_window_slot 的 `reserved` 去重写在 FakeLinkGate 里，断言的是假件自身行为，不验证守护进程；按 ports 文档幂等由 gate 承担，代码每次读都调 admit_dispatch 符合契约，但该测试不构成证据。
- gate.admit_dispatch 抛异常时 RuntimeError 直接逃出 IPC（探针 5）；WP4 LinkAdmission 对 dispatch 异常 fail-closed 并记 link.gate_error。

B2-R4 真实 IPC fence：PASS。_trusted_message_origin="ipc" 下未认证 → SENDER_NOT_AUTHENTICATED，observe 无 fence 与伪造 fence → STALE_SESSION，被顶替 → SESSION_SUPERSEDED，均断言 inbox.fetches 为空、publisher 计数 0；有效 worker fence 计数 1；read_body 与 settle_hint 的 owner onBehalfOf 及他人消息均有测试。「过期 fence」未单列，fence 走共享 helper，接受。

B2-A3 Stage A 缺 summary 仍成功：PASS（send 与 reply 各一条）。

裁定 39 replyTo 非空字符串：PASS（空串/int/list 三例先红后绿，INVALID_ARGUMENT，未入 outbox）。
gate 未绑定日志 link.gate_unbound：PASS（事件名与 WP4 相同；未做 WP4 的每接收方每分钟限频，合并后改用 WP4 LinkAdmission 即消除）。
裁定 55 message.reply 写 replyTo：本分支（3e2b20926 起）对所有回复无条件写 `reply_body["replyTo"] = message_id`，是 WP2 §2.4 第 6 条要的完整形态，不是半做；WP2 实现时须基于此行，避免同行异改冲突。
message.settle_hint 幂等：PASS。只读，不 ack、不盖章、不发回执；非 pending → ack，无 gate → keep，gate 非法答复 → keep 并记日志，与 ports 文档「Never changes state」一致。
迁移：PASS。test_the_three_tables_gain_summary_and_keep_legacy_rows 重开已迁移库不重复 ALTER；summary 经 zenoh 帧（inbox/links/zenoh_link.py 可选键）、outbox 重试、custody 路径往返均有测试。
先红后绿：PASS。2addf6bf6 测试在 b0b65c418 代码上 5 failed / 69 passed，HEAD 74 passed。
回归：焦点五目录 952 passed / 1 failed（ipc cost 计时比在 -n auto 下 0.66，串行重跑 27 passed）。tests/daemon tests/shell：7980 passed / 8 failed；pi controlled_loader t04-06 为 dev 既有；5 个仅并行失败、串行通过；test_shell_cli_entry 命令清单与 login/device t11 在 b0b65c418 上同样失败（来自集成分支 links/ 与 shell，本包未改）。wait_without_polling 本次未失败。
门禁：structural PASS，module boundaries PASS，ruff PASS，git diff --check b0b65c418 HEAD 干净。

## 合并试算

对 wp4/send-links（8c720faf6）冲突 6 个文件：
- dispatch/admission.py 4 处：L19 WP4 新增 import（DISPATCH_SERVICE_ACTOR_NAME、LinkGate）对本分支空；L108 WP4 的事件常量与 E2E 开关对本分支中部的 `__all__`；L150 link_governed 豁免行（sender-only 对 sender-or-recipient）；L162 WP4 的 LinkAdmission 类与尾部 `__all__` 对本分支空。即使函数体逐字相同，`__all__` 位置与 import 仍冲突；裁定 38 已定 WP4 先合，admission.py 整文件取 WP4 即可。
- inbox/contracts/ports.py、service/projection.py、actor/custody/lifecycle.py、authority/facade.py：FetchPendingCommand 上 WP4 加 `only: tuple[str,...] | None`（候选集，准入在 actor 内），本分支加 `message_id: str | None`（单行收窄）。语义重叠，是真接缝不是文字冲突：read_body 应改用 WP4 的 `only=frozenset({id})` 与 actor 线程准入。
- service/delivery/receive.py：WP4 的 ingress 拒收包裹对本分支同段，机械合并。
- ipc/router.py 无冲突。

对集成分支（0ec2bd8fe，返工基线之后 WP7 已合入）冲突 2 个文件：
- inbox/service/core.py：WP7 新增 system_notices.summary 的 ALTER 对本分支在 outbox/inbox/custody 循环内的 summary ALTER；两段都要保留。
- inbox/service/delivery/retry.py：summary 解码上方注释两版互斥；应改为四张表都带该列。

## 接受的偏差
- R4 未单列过期 fence（共享 fence helper，已有被顶替与伪造）。
- link.gate_unbound 未限频（随合并改用 WP4 LinkAdmission）。
- 远端 dispatch-coordinator 不豁免，与 WP4 当前实现一致，归 WP4 二次验收（裁定 55）。

## 交给 WP8 的契约清单（本包不改 contract/）
- 新 IPC 方法 message.read_body、message.settle_hint（入参 messageId；返回 ok/messageId/conversationId/from/to/intent/summary/message/bodyBytes/origin/pending/fetched；settle_hint 返回 hint ∈ {ack,keep}、pending）。
- worker_channel.METHOD_PARAMS：send/reply 增 summary，新增两方法（裁定 41：与 WP6 同批合并，同时删 test_mcp_delegate_removed 对 read_body 的 METHOD_NOT_FOUND 放行）。
- message.send/message.reply 入参 summary；send 的 replyTo 非空字符串校验（INVALID_ARGUMENT）。
- message.pending.list 行形状 kind/summary/bodyBytes。
- 错误码 SUMMARY_REQUIRED（data {summaryBytes,maxBytes}）、MESSAGE_BODY_UNAVAILABLE（data {messageId}）。
- 线框 envelope 可选键 summary（zenoh 帧）；reply body 对 agent 目标新增 replyTo 键（digest 变化，裁定 39 同理）。
- ipc/wire.py 的 summary 映射归 WP3-B（裁定 40）。

## 返工清单
1. link_governed 改成与 WP4 8c720faf6 逐字相同（sender 或 recipient 为内部 actor 即不受管）。推荐做法：按裁定 38 等 WP4 合入集成分支后合并集成分支，admission.py 取 WP4 版，read_body 改调 `self._link_admission.governs(...)` 并复用其内部 actor 集合，不在 queries.py 另建集合；修正 `_admit_read_body` docstring。
2. 关闭 retained-row 回退泄漏：非 pending 分支对受管行必须证明该行曾被准入（例如已 fetched 或 consumed 经正常投递），否则返回 MESSAGE_BODY_UNAVAILABLE 且不报告是否存在；过期且从未准入的受管行同码。加测试：admit=False 下 ack 后读、过期后读，均断言 MESSAGE_BODY_UNAVAILABLE 且 gate 调用与行状态不变。
3. gate.admit_dispatch 抛异常时 read_body fail-closed：MESSAGE_BODY_UNAVAILABLE 加 link.gate_error 日志（与 WP4 同事件名），加测试。
4. test_repeated_reads_reserve_one_window_slot 改为断言守护进程侧可观察结果（重复读不再盖章、不重复发 fetch receipt），或删去「reserved」断言。
5. WP4 合入后：read_body 的单行拉取改用 WP4 的 FetchPendingCommand.only 与 actor 线程准入，删除本分支的 message_id 收窄，消除 ports/projection/lifecycle/facade 四处接缝。
6. 合并集成分支 0ec2bd8fe，解决 core.py（保留 WP7 system_notices.summary 与本分支三表 summary 两段 ALTER）与 retry.py（注释改为四表）冲突，重跑迁移与 summary 往返测试。

## 集成者补充（2026-10-15）：返工 3 清单（基线 6999b6ab6，已合入 WP4 后的集成分支 fe4890146）

集成者已做：6 处冲突全部取集成侧（WP4 的 `FetchPendingCommand.only`、保留名 `link_governed`、`LinkAdmission`）；`queries.py` 的 `fetch_pending(key, message_id=…)` 改为 `fetch_pending(key, only=frozenset({message_id}))`，`_admit_read_body` 改为 `self.link_admission.governs(...)`；`receive.py` 补回 `summary` 列。合并后 7 条 WP3-A 测试红（见下），其余 508 绿，结构门清。

1. 把 7 条红测试改到 WP4 的 API 语义而**不削弱断言**：`test_call_paths.py::test_link_governed_exempts_an_internal_actor_in_both_directions`（按保留名、不限节点，远端 `dispatch-coordinator`/`_turn-hooks` 也豁免，近似名仍受管）；`test_pending_shape.py::test_the_a_phase_row_adds_summary_and_body_bytes`；`test_read_body.py` 三条（单行盖戳且保留首次戳、IPC fence、owner on-behalf-of）；`test_fetch_one.py` 两条改为 `only=frozenset({id})` / `FetchPendingCommand.only`。
2. 唯一 gate 槽（README 裁定 67）：`read_body`/`settle_hint` 用的 gate 必须来自 `self.link_admission.gate`；若本分支在 `DaemonApplication` 上另设了 `_link_gate` 属性，改为只读别名 `return self.link_admission.gate` 或删除，不得出现第二个绑定点；加一条测试：`link_admission.bind(gate)` 之后 `read_body` 走该 gate。
3. 二次验收返工 2、3（保留行回退泄漏：ack 后再读、过期行；gate 抛错 fail-closed）：核对取消前 worker 的提交 e7084cbfa/15278cf04/019f8ffda 是否已覆盖，列出测试名并确认绿；缺的补齐（先红）。
4. 二次验收返工 4：重复读测试必须断言 daemon 行为（第二次 read 不再调 gate、不再盖戳），不能靠 `FakeLinkGate` 内部去重。
5. 证据：`docs/evidence/org-structure-constrain-2026-10-14/wp3a/` 下记录先红（在 6999b6ab6 上跑第 1 条的 7 红输出）与转绿；命令：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/application/transport/channel/summary tests/daemon/inbox/summary tests/daemon/links tests/daemon/dispatch tests/daemon/inbox tests/daemon/delivery tests/daemon/application/messaging tests/daemon/ipc/error -q`，再 `tests/daemon tests/shell -q`（既有红：pi controlled_loader t04-06、ipc cost heartbeat、kernel POSIX listener fd、shell CLI command inventory、login/device t11）；结构门、模块边界门、`ruff check .`、`git diff --check 6999b6ab6 HEAD` 为空。不改 ports.py，不动 orgfs，只 git add 具体文件，测试提交在实现提交之前。
