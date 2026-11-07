# WP4 第二次验收（opus，2026-10-14）：REJECTED，返工 1 条

验收对象：wp4/send-links @ 8c720faf6（返工提交 4a4ab8d65 测试先红 → 4aca830ed 修复 → 8c720faf6 证据）。
返工基点：195d49f01（合入 refactor/org-structure-constrain 的 merge；第一次验收对象 c37852d4a 是它的祖先）。
环境：macOS，worktree 自带 .venv，一律 `uv run`。

结论：第一次验收的 10 条全部落实。被拒原因只有一条：README 裁定 45/51（第 55 条明确要求本次验收核）要求内部 actor 按保留名判、不限节点，现实现仍按本节点完整 URI 判，且测试把相反语义钉成了期望。

## 第一次验收 10 条逐条

1. 先红证据 — PASS。4a4ab8d65 只改 tests/（11 个文件，无 src）。在 scratchpad 以 `git worktree add --detach .../red 4a4ab8d65` 取出，`PYTHONPATH=red/src uv run --project <wp4> --extra test pytest tests/daemon/links/admission -q` → 9 failed, 60 passed，9 条与证据文件所列逐条一致，均为行为断言（AssertionError），非 ImportError。HEAD 同命令 → 69 passed。B2/B3 行为级红不可得的说明（已在被拒实现中修好）属实，接受。
2. Contract-Impact — PASS。证据文件含 `Contract-Impact:` 行与受影响 contract/ 文件清单；分支未改 contract/（`git diff --name-only 210287b40 HEAD -- contract` 为空）；`check_contract_impact.py --base 210287b40 --head HEAD` → candidates=0 impacts=0（该门只抓删除/改名，新增的 `state="refused"`、`LINK_UNKNOWN`、`ConsumptionState.refused` 是追加语义，contract/ 现无相关枚举）。由 WP8 同步。
3. 唯一缺省 — PASS。`git grep -n "link_admission or LinkAdmission(" -- src` 恰 2 行（inbox/service/core.py:79、runtime/bridge.py:215）；`git grep "_link_admission is None" -- src` 无输出；LinkAdmission 无 `__len__`/`__bool__`，`bool(LinkAdmission())` 为 True，`or` 不会替换传入的槽。
4. B1.5 回复边界 — PASS。test_reply_boundary_never_exposes_denied_rows 参数化 missing/wrong-type/empty/other-peer/closed/reclaimed；list、wait、headless 三入口各有测试（test_reply_boundary_on_list_and_wait、test_wait_does_not_ring_for_denied_replies、test_valid_reply_passes_headless_while_w_is_full）。
5. B3.4/B3.5 线程 — PASS。test_wait_gate_calls_run_on_the_inbox_actor_thread、test_headless_gate_calls_run_on_the_inbox_actor_thread、test_concurrent_entry_points_share_one_admission_budget（真线程）均在 HEAD 通过。
6. B4.4/B4.5 — PASS。test_rolled_back_refusal_emits_no_notice、test_refusal_notice_dedups_at_the_receiving_node（断言接收端 ReceiveResult.duplicate）、test_many_refusals_never_drop_a_notice（红：256 == 300；绿）；core.py 暂存改为无界 `deque()`。
7. B5 — PASS。test_offer_coordinator_head_is_held_without_blocking_others（actor_mode=True 生产 offer coordinator）、test_reclaimed_row_is_not_revived、test_waiting_row_expires_without_dispatch_or_resend、test_window_shrink_and_release_re_evaluates、test_budget_is_shared_across_different_senders 均通过。
8. B6 — PASS。views.py 改为 inbox 行 + `governs` − `admitted_message_ids`；read.py 加 `fetched_at_ms IS NULL`；ack（retry.py）与 TTL prune（projection.py）调用 `forget`；三条红测试转绿。
9. T17 — PASS（附注）。test_real_composition_passes_one_link_admission_to_both_owners 走真实 `DaemonApplication._start_runtime`，断言 coordinator 的 `service_options["link_admission"]` 与 bridge 的 `link_admission` 均 `is app.link_admission`。未断言「进程内只有一个 LinkAdmission 实例」，但两处默认只在传入 None 时触发、而两处都拿到了同一对象，等价成立，接受。
10. 测试替身移出产品 — PASS（附注）。`git grep -n "admit_send\|settle_hint" -- src/hyprial/daemon/impl/dispatch src/hyprial/daemon/impl/runtime src/hyprial/daemon/impl/inbox` 无输出；替身在 tests/support/links/gate.py；读取处打 `link.e2e_permissive_gate` warn。

## 本次附加检查

- 裁定 45/51（保留名、不限节点）— FAIL。application/__init__.py:764-771 把 `internal_actors` 组成本节点完整 URI `{dispatch_service_actor_uri(owner, node_id), canonical_agent_uri(owner, node_id, "_turn-hooks")}`，`link_governed` 以 `sender in internal_actors or recipient in internal_actors` 比对完整 URI。探针（HEAD，internal 为 node-a 的两个 URI）：
  `agent:op:node-b:dispatch-coordinator -> agent:op:node-a:w governs= True`
  `agent:op:node-a:w -> agent:op:node-b:dispatch-coordinator governs= True`
  `agent:op:node-b:_turn-hooks -> agent:op:node-a:w governs= True`
  三者按裁定都应为 False。tests/daemon/links/admission/test_link_admission_slot.py 的 test_link_governed_matrix 末项注释「Same actor name under another owner/machine is a different identity」并期望 `canonical_agent_uri("other", "elsewhere", "dispatch-coordinator") -> B` 为 governed=True，与裁定 45/51 相反。
- 裁定 26/54 — PASS。未绑定：ingress 接收（ingress_refusal 返回 None）、dispatch 持有（dispatch_admitted 返回 False），与 54 的「发送方 hold，接收方不派发」一致；WP4 未触碰发送路径，无矛盾。
- 裁定 37 — PASS。`user:` 目标与发送方均不受管（matrix 中 `(A, user:op) -> False`、`(user:op, B) -> False`）。
- 裁定 38 — PASS。`git grep -n "def link_governed" -- src` 仅 dispatch/admission.py 一处。
- ports.py 未改（`git diff 195d49f01 HEAD -- src/hyprial/daemon/impl/links/ports.py` 为空）。
- 焦点：`uv run --extra test pytest tests/daemon/dispatch tests/daemon/delivery tests/daemon/links tests/daemon/ipc/error -q` → 393 passed。证据声称的 `tests/daemon/links tests/daemon/inbox tests/daemon/delivery tests/daemon/dispatch` → 514 passed，复现。
- 全量 daemon：`uv run --extra test pytest tests/daemon -q` → 5 failed, 6448 passed, 5 skipped, 5 xfailed, 1 xpassed, 1 error（18:23）。逐条判定：
  - pi controlled_loader t04-t06：dev 既有红。
  - ipc cost test_four_sides_cover_a_pure_heartbeat_load：单跑 HEAD 3/3 失败、返工前（red worktree，src=195d49f01）3/3 失败，既有红，非回归（证据所说「单跑通过」在本机不成立）。
  - progress test_worker_progress_reaches_sender_without_becoming_a_prompt、local_first 两条、session_persistence_overload 一条 ERROR：HEAD 单跑全部通过（14 passed），属负载下不稳定，非回归。
  - wait_without_polling 本次未出现。
- 结构门：check_structural_limits.py exit 0，violations []；check_module_boundaries.py exit 0。
- `uv run ruff check .` → All checks passed!
- `git diff --check 195d49f01 HEAD` → 无输出。
- 合并就绪：`git merge-tree --write-tree refactor/org-structure-constrain HEAD`（ref = 0ec2bd8fe）→ 8a2c30da6cb1ed4ebe2eb3fb9bd59f18dcc81466，退出 0，无冲突。

## 接受的偏差

- 第 10 条：产品代码在 env 开关下动态 `from tests.support.links.gate import E2EPermissiveLinkGate`。产品依赖测试包不理想，但开关仅隔离 E2E 使用、读取处 warn、导入失败也 warn 并保持未绑定（fail-closed），且 WP2 接线后删除。接受，WP2 合入时必须删除该段。
- 第 9 条未数实例，见上。
- 建议项（held_message_ids 加锁/快照）：`held_message_ids`/`admitted_message_ids` 用 `tuple(self._admitted.items())` 快照，IPC 线程读、actor 线程写时仍有 OrderedDict 变更期间迭代的理论窗口；非阻断。两条不写 body replyTo 的回复路径未在证据中记载（第一次验收要求「证据里要记」），非阻断，由 WP3-A/WP5 处理。
- 证据文件 docs/evidence/wp4-send-links-implementation-2026-10-14.md 位于 docs/evidence/ 根，不在 docs/evidence/org-structure-constrain-2026-10-14/；开头 Local commit 仍写 892f765c6。不阻断，集成者归档时移动。

## 集成者在合并时补（WP2 前置，不作拒绝理由）

LinkAdmission（src/hyprial/daemon/impl/dispatch/admission.py）现状：`governs(sender, recipient)` 已存在（189 行起）；`internal_actors` 与 `gate` 只读属性不存在（只有私有 `_internal_actors`、`_gate` 与 `bound`）。在 `bound` 属性后补：

```python
    @property
    def internal_actors(self) -> frozenset[str]:
        return self._internal_actors

    @property
    def gate(self) -> LinkGate | None:
        return self._gate
```

若按下方返工把 `internal_actors` 改为保留名集合，WP2 复用的就是保留名集合（与裁定 45「WP4 把内部 actor 集合暴露为只读属性，WP2 复用」一致）。另注意：保留名判定意味着任何节点上名为 `dispatch-coordinator`/`_turn-hooks` 的 actor 都豁免，集成者需确认 agent 注册拒绝这两个保留名（本次未在 src 中找到保留名拒绝逻辑）。

## 返工清单

1. 【裁定 45/51/55】`link_governed` 改为用 `parse_agent_uri` 取 sender 与 recipient 的 actor 段，与保留名集合 `{DISPATCH_SERVICE_ACTOR_NAME, "_turn-hooks"}` 精确比对（不比 owner、不比节点；仍不做前缀匹配，保持 B2：`_named-worker` 受管）。composition（application/__init__.py:764-771）传保留名而非本节点 URI，或 `internal_actors` 语义改为名称集合并同步签名注释。测试：先提交只含测试的提交使下列断言为红，再修复——matrix 中 `agent:other:elsewhere:dispatch-coordinator -> B` 改为 False；补 `A -> agent:op:node-b:dispatch-coordinator`、`agent:op:node-b:_turn-hooks -> A` 为 False；ingress 补一条远端节点 `dispatch-coordinator` 发来不经 `sender_known` 的测试。验收命令：`uv run --extra test pytest tests/daemon/links/admission -q` 全绿，上方三条探针输出 governs= False。

## 集成者补充（2026-10-14，README 裁定 63）：返工清单最终版

1. `link_governed` 按保留名判内部 actor：用 `parse_agent_uri` 取 actor 段，与 `DISPATCH_SERVICE_ACTOR_NAME`、`"_turn-hooks"` 精确相等（不做前缀匹配），不限 owner/node；sender 与 recipient 任一命中即不受管。`application/__init__.py:764-771` 的 `internal_actors` 改为保留名集合（或保留 URI 集合但判定改为按名）。`test_link_governed_matrix` 末项期望改为 False，并新增远端节点三例（上文探针的三条）先红后绿。
2. `LinkAdmission` 增两个只读属性（放在 `bound` 之后）：`internal_actors -> frozenset[str]`、`gate -> LinkGate | None`。各一条测试。
3. 保留名注册守卫：identity 的 agent 注册入口（`identity/impl/agents/registry/_core.py` 的创建/注册路径）拒绝 actor 名等于 `dispatch-coordinator` 或 `_turn-hooks`（精确相等），返回现有的无效名错误码；一条先红测试。没有这条守卫，任何人起一个叫 `_turn-hooks` 的 agent 就绕过全部 link 约束。
4. 证据文件移到 `docs/evidence/org-structure-constrain-2026-10-14/wp4/`，表头改为本次返工基点；补记第一次验收要求的两条省略 body `replyTo` 的回复路径。
