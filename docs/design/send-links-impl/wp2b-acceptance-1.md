# WP2-B1 / WP2-B2 第一次验收：REJECTED

- 分支：`wp2b/send-links`，worktree `/private/tmp/hf-wp2b-send-links`，HEAD `8d03238eb`
- 基线：`0e552780d`（wp2 规格 v2 §9.5）；B2 的规格提交为 `a75cccec0`（§9.6）
- 验收人：opus 验收角色，2026-10-15
- 规格依据：wp2.md v2 全文（§9.3–§9.6 优先于正文），README 裁定 26/37/39/46/47/53/54/55/56/63/67/68/70
- 结论：REJECTED。共 3 条返工，均属 B1/B2 范围（见文末）。另有 2 条引擎缺陷，归 WP2-A，不计入本包返工。

## 一、逐条判定

| # | 条目 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1a | 范围：`links/` 与 `application/__init__.py`、`wiring/transport.py`、`inbox_surface/queries.py`、`dispatch/admission.py`、`hooks/service.py` 的 diff | PASS | `git diff 0e552780d HEAD --stat -- <这些路径>` 输出为空 |
| 1b | 文件数与行数门 | PASS | `inbox_surface` 7 个文件；`inbox/service` 7 个文件；改动的 `.py` 最大为 `read.py` 933 行，`send.py` 905 行 |
| 2a | B1 同事务写入点（submit、投递/托管/TTL、DLQ、入站回复、注入、ack、过期） | PASS | 读码：`core.py::_insert_outbox` 的 ledger 写入在同一次 `commit` 之前；`outcomes.py`、`delivery/retry.py`、`receive.py`、`projection.py` 的写入都在原事务内。L1–L13 共 14 条全部通过 |
| 2b | 迁移在已迁移的库上幂等 | PASS | 探针：同一个库连续打开 3 次，无重复 ALTER，held 行保持不变 |
| 2c | hold 行被 retry 泵跳过，且只放行一次 | PASS | 探针：`retry_due` 不选 hold 行；对同一 id 两次 `release`，结果依次为 `released=('h1',)` 与 `()` |
| 2d | 写入后、放行前崩溃，状态仍一致 | PASS | 探针：不调 close 直接丢弃 service 后重开，outbox `link_hold` 与 ledger `held` 一致；放行后 `held→open` |
| 2e | held 回复放行时同事务 ack 原请求（§3.6、L8、B5） | FAIL | 探针 P1：经 `message.reply` 被 hold 的回复，用 `update_link_ledger(release=...)` 放行后结果为 released，但原请求仍在 `pending_messages`。原因：B2 的 hold 回复不写 ledger `out` 行，而 B1 的 `_release_one_locked` 只从 ledger `out` 行取 `reply_to`。L8 自己构造了 `link_open`，所以掩盖了这条接缝 |
| 3a | 受管判定（`governs`）先于询问 gate | PASS（附注） | agent 目标与 reply 路径都先调 `_link_governs`；自发与内部 actor 不询问 gate（W3 exempt 测试）。`user:` 目标按 §2.4 第 4 步与裁定 53 有意询问 gate，因为 W 由 gate 判。附注：`_link_reply_unavailable` 在原请求不存在时不经 governs 就询问 gate，后果只是错误码由 gate 决定，可以接受 |
| 3b | Allow/Mint/Hold；`holdReason` 只出现四个码；`"reason"` 键不带 LINK_* 码 | PASS | W3 与探针 P7（PEER_FULL/SELF_FULL/WINDOW_FULL）：delivery 与顶层都带 `holdReason`。`"reason"` 键只有既有的 `target-not-visible` 文本 |
| 3c | 裁定 53：非 owner `user:` 在 W 满时同步拒绝；owner 不拒；回复 user 永不拒 | 边界 PASS / 集成 FAIL（引擎） | 边界原样传播 gate 抛出的 `LINK_WINDOW_FULL`，不入 outbox，不写 ledger（W3）。真实引擎探针 P10：owner 得 `Allow`，**但对非 owner user 的回复在 W 满时被拒为 `LINK_WINDOW_FULL`**。原因在 `links/service.py` 的 W 检查不看 `reply_to`，归 WP2-A（与 wp2a-acceptance-1 一致） |
| 3d | 裁定 54：未绑定时 hold `LINK_SELF_FULL`/`gate-unavailable`，绑定后第一次 pass 放行 | hold PASS / 放行 BLOCKED | W3 unbound 测试与读码确认 hold 部分。放行依赖 composition 的 `LinkInboxPort` 与 `service.start()`，在 B3 范围，本分支无法实测 |
| 3e | 裁定 55：agent 目标的回复 body 写 `replyTo`；裁定 39 的校验 | PASS | W4 两条；探针 P8：`replyTo=""` 与 `replyTo=5` 都得到 `INVALID_ARGUMENT` |
| 3f | 裁定 70：对已结算 id 的询问不占 W | 发送侧 PASS / dispatch 侧 FAIL（引擎） | 发送侧：对已结算 id 回复时，引擎抛 `MESSAGE_REPLY_UNAVAILABLE`，不预留。dispatch 侧探针 P9：m1 acked 并对账后，`unclosed=0`；再次 `admit_dispatch(m1)` 返回 True，m1 重新进入 `pending_inbound`，`unclosed=1`。归 WP2-A |
| 3g | 每个被接受的发送恰好调用一次 `LinkSendObserver.submitted` | PASS | 探针 P6：Allow、Mint、owner user、reply 各调用 1 次；submit 被拒时调用 0 次 |
| 3h | 同一操作键的幂等重试只产生一次 gate 决策、一行 outbox | agent PASS / user FAIL | 探针 P5（agent，Allow）：gate 1 次、submitted 1 次、outbox 1 行。探针 P2（`user:` 目标）：重放时再次询问 gate（共 2 次）；若此时 W 已满，重放被拒为 `LINK_WINDOW_FULL`，没有返回原回执，违反 B6 与 §2.4 第 2 步 |
| 4 | 12 个被改的既有测试文件只改替身与夹具，不削弱断言 | PASS | 逐文件 diff：只有 `submit(**_link)` 签名、显式 `PermissiveLinkGate` 绑定（§4.9 方案 a），以及两处新增 `replyTo` 断言（是加强）。无删除或放宽的断言 |
| 5 | 先红 | PASS | `5158949da` 与 `86a02f5c7` 都只含测试文件。在 scratch worktree 中复现：ledger 14 failed（行为失败，不是收集错误）；wiring 9 failed、2 passed（两条守卫测试在基线上本来就绿） |
| 6a | 焦点集 | PASS | `tests/daemon/links tests/daemon/inbox tests/daemon/ipc tests/daemon/delivery tests/daemon/application/messaging tests/shell/cli/lifecycle/test_cli_top.py`：823 passed（worker 报的 797 不含 messaging） |
| 6b | `tests/daemon tests/shell tests/integration` | PASS（只有已知红） | 4 failed、10200 passed、11 skipped、5 xfailed、1 xpassed。4 条红是 pi controlled_loader t04-06、ipc cost heartbeat、shell CLI command inventory、login/device t11。worker 报的 18 条 `squire/setup` 失败未复现 |
| 6c | 结构门 | PASS | 恰好 1 条违规：`links/service.py` 2011 行，属 WP2-A，本包未新增违规 |
| 6d | 模块边界 / ruff | PASS | `check_module_boundaries` 退出 0；`uv run ruff check .` 输出 All checks passed |
| 6e | `git diff --check 0e552780d HEAD` | PASS（附注） | 代码干净。只有两个证据日志 `red-wp2b-ledger.txt` 与 `red-wp2b2-wiring.txt` 带 pytest 原样输出的行尾空白，共 41 处 |
| 7 | merge-tree | 见第四节 | |
| 8 | Contract-Impact | 见第五节 | |
| 9 | 裸 `python3` 失误 | PASS | 工作树干净；`git status --ignored` 中无 `.bak`/`.orig`/`.rej`/`.tmp` 残留；该次替换未 import 产品代码 |

## 二、探针结果（scratch 文件，未提交）

- P1（FAIL，B1/B2 接缝）：hold 回复放行后，原请求 `req-h` 仍为 pending。ledger 快照为 `()`，说明回复没有 `out` 行。之后 retry 泵会投递该回复，原请求却一直不 ack。
- P2（FAIL，B2）：`user:` 目标用同一个 `idempotencyKey` 重放时，第二次仍询问 gate。gate 改为 W 满后，重放抛 `LINK_WINDOW_FULL`。
- P3（FAIL，B2，次要）：`user_delivery.deliver` 抛异常时，ledger `out` 行仍为 open，泄漏到 TTL；`UserDeliveryTarget.parse` 失败时同样泄漏。§2.4 第 6 步要求同一请求内释放。
- P4（无回归）：同一键、不同正文的重放在基线（86a02f5c7）与 HEAD 上都静默返回原回执。差别在于 HEAD 的重放回执不再带首次响应的 `code`/`reason`（`TARGET_UNRESOLVED`/`target-not-visible`），只带 `accepted`/`queued`。
- P5、P6、P7、P8：PASS，见上表。
- P9（引擎，WP2-A）：裁定 70 在 dispatch 侧不成立。
- P10（引擎，WP2-A）：W 满时，对非 owner user 的回复被拒。
- B1 崩溃与只放行一次：PASS。

## 三、失败分类

| 失败 | 分类 |
| --- | --- |
| P1：hold 回复放行时不 ack 原请求 | B1-B2 引入（接缝） |
| P2：`user:` 重放再次询问 gate | B2 引入 |
| P3：`user:` ledger 行泄漏 | B2 引入 |
| P9：dispatch 重问已结算 id 时占 W | 引擎（WP2-A），由 B3 接线后暴露 |
| P10：W 满时回复 user 被拒 | 引擎（WP2-A），已在 wp2a-acceptance-1 记录 |
| pi controlled_loader t04-06、ipc cost heartbeat、shell CLI inventory | dev 既有红 |
| login/device t11 | 集成分支引入（WP1 新增 daemon 文件未分类，归 WP8） |
| `squire/setup/*` 18 条（worker 报） | 环境：本次未复现 |
| 合并后的 4 条红：`test_schema_migration` 多出 `link_hold` 列；`test_read_body` 两条 `link gate is already bound`；`test_settle_hint_acks_after_a_successful_reply` | 集成交互（B2×WP3-A），与 61dbd420a 中记录的 4 条一致 |

## 四、merge-tree

- `git merge-tree --write-tree refactor/org-structure-constrain HEAD`（集成头 `61dbd420a`）：1 处冲突，在 `src/hyprial/daemon/impl/inbox/service/core.py::_insert_outbox`。WP3-A 加 `summary` 列，本包加 `link_hold` 列，冲突有两块：列名行与值元组。解法是两者都保留，并把 `VALUES` 占位符从 14 个改为 **15 个**。两侧都改成了 14 个占位符，所以这一行会自动合并成 14 个，必须手改。按此解决后：焦点集 1 failed / 863 passed，3 条 summary 测试另外 3 failed，合计即上表的 4 条交互红。
- `git merge-tree --write-tree wp2/send-links HEAD`：无冲突（tree `bf08073cf`）。

## 五、Contract-Impact（交 WP8）

1. `message.send` 的 delivery 与单目标顶层新增 `queued: true`、`holdReason`（四个码之一）、`detail`；Mint 时还有 `evicted`。新 detail 值：`minting`、`gate-unavailable`（裁定 54）。
2. `message.reply` 被 hold 时返回 `{ok: true, messageId, replyMessageId, replied: false, acknowledged: false, queued: true, holdReason, detail}`。hold 但 submit 未被接受时返回 `ok: false`，同时带 `code` 与 `holdReason`。
3. 同步拒绝：错误码 `LINK_WINDOW_FULL`，data 为 `{target, detail: "W"}`（由引擎抛出，边界原样传播）。
4. `replyTo`：agent 目标的回复 body 新增 `replyTo`（digest 改变，裁定 39/55）。`message.send` 带 `replyTo` 时 body 也新增该键。`replyTo` 为 `""` 或非字符串时，由按请求处理改为 `INVALID_ARGUMENT`。
5. `outbox list`：`OutboxItem.link_hold` 已有，但 `queries.py` 冻结，IPC 条目**还没有** `holdReason`。L2 要求的 `outbox.list` 行含 `holdReason` 未兑现，须在 B3 或集成时补。
6. 重放回执的形状比首次响应少 `code`/`reason`/`openRequestsFrom`。

## 六、接受的偏离

- W6 只覆盖 CLI 两种来源。`onBehalfOf` 在本构建中只做署名，所以 `XS → CALLER_NOT_AUTHORIZED` 改成了 `SENDER_UNRESOLVED` 守卫测试。interactive、adapter 与 daemon 内部来源留给 B3。
- W5 只覆盖 held send 重放；W 满重放、已 ack 回复重放、digest 冲突未单独成测（digest 行为由探针 P4 确认与基线一致）。
- gate 未绑定时，回复也按裁定 54 hold（fail-closed），规格未写明但方向一致。
- 证据目录里两个 `module-boundaries-*.json` 各约 15 万行，属于证据膨胀，建议集成时删去或压缩。
- `user:` 路径对非 agent 发送者（adapter/user）也会写 ledger `out` 行。引擎会忽略这些行，但它们污染 ledger，建议返工时一并只对本节点 agent 写。

## 七、集成者合并时要办

1. `core.py` 冲突：列名写成 `summary, link_hold`，值元组两项都保留，`VALUES` 改为 15 个占位符。
2. `test_schema_migration` 的期望列表加 `link_hold`。
3. `test_read_body` 两条与 `test_settle_hint_acks_after_a_successful_reply`：共享的 `application()` 夹具已绑定 `PermissiveLinkGate`，WP3-A 测试再绑自己的 gate，就会出现 `already bound`。改法：WP3-A 测试改用未绑定的夹具，或先解绑、换槽。
4. `outbox list` 的 `holdReason`（第五节第 5 条）。
5. 引擎缺陷 P9、P10 转 WP2-A 返工。

## 八、返工清单

1. **hold 回复的放行必须 ack 原请求（P1）。** 在 B1 的 `_release_one_locked` 与 `_fail_reply_locked` 中，从 outbox 行本身取回复关联：`intent == "reply"`，`reply_to` 取 payload 的 `replyTo`（B2 已对 agent 目标写入），缺失时取 `idempotency_key` 的 `reply:` 前缀，actor 取 outbox 的 `sender`。不再依赖 ledger `out` 行。不要改成让 B2 给回复写 `out` 行：回复的 `out` 行放行后变为 `open`，之后不会被闭合，会被当作出站在途。补一条测试：经 `message.reply` 得到 hold，再 `update_link_ledger(release=...)`，断言原请求 consumed、`in` 行 closed，且两者与清列在同一事务。
2. **`user:` 目标的重放不得再问 gate（P2，B6）。** 询问 gate 之前，先识别重放：本节点已有该 `(sender, message_id)` 的 ledger `out` 行，或 `user_delivery` 报 duplicate。识别为重放时返回原结果，不预留，不重复调用 `submitted`。补测试：首发之后 gate 改为 W 满，同键重放仍得到原结果，gate 调用次数不变。
3. **`user:` ledger 行在任何失败路径都要释放（P3）。** 把 `UserDeliveryTarget.parse` 失败与 `deliver` 抛异常纳入释放，例如用 try/except 包住，失败时调用 `_link_user_release`。只对本节点 agent 发送者写 `out` 行。补测试：deliver 抛异常后，没有存活的 `out` 行。

## 集成者补充（2026-10-15 09:xx）：返工清单（B1/B2 范围，3 条）

1. 被 hold 的回复放行后必须闭合原请求：`message.reply` 遇 gate `Hold` 时不写 ledger `out` 行，而 B1 的 `_release_one_locked` 只靠该行找 `reply_to`。改为从 outbox 行自身推导关联（intent=reply、payload `replyTo` 或 `reply:` 幂等键、sender），放行时闭合原请求并 ack；新增一条从 `message.reply`（Hold）→ 放行 → 原请求不再 pending 的端到端测试（先红）。
2. `user:` 目标的幂等重放不得再次询问 gate：同 `idempotencyKey` 重放返回原回执（即便此时 W 已满），gate 调用计数为 1；对齐 agent 目标现有行为；测试先红。
3. `user:` 的 ledger `out` 行在投递抛错或 `UserDeliveryTarget.parse` 失败时必须回滚/关闭，不得留到 TTL（§2.4 第 6 步）；两例测试先红。

不在本次范围（已另行归属）：引擎两缺陷（裁定 70 已结算 id 再准入、W 满时对非 owner user 的回复被拒）归 WP2-A；`outbox list` 的 `holdReason` 字段与 4 条 B2×WP3-A 交互红由集成者在合入时处理。只改 `inbox_surface/link_send.py`、`inbox/service/link_ledger.py` 及 ledger 相关 inbox 文件、测试与证据；不改 `links/`、`queries.py`、`application/__init__.py`、`transport.py`。
