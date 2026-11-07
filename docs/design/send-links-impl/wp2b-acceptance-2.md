# WP2-B1 / WP2-B2 第二次验收：REJECTED

- 分支：`wp2b/send-links`，worktree `/private/tmp/hf-wp2b-send-links`，HEAD `db9fa6f8d`
- 返工区间：`1484449bd..db9fa6f8d`（`b038630e0` 先红测试，`c0260a5f6` 修复，`db9fa6f8d` 证据）
- 依据：wp2b-acceptance-1 的「集成者补充（2026-10-15 09:xx）」3 条返工；wp2.md §2.4 第 2、6 步与 B5、B6；README 裁定 53/54/55/70
- 验收人：opus 验收角色，2026-10-15
- 结论：REJECTED。第 1、3 条 PASS。第 2 条 FAIL：重放识别改用 ledger `out` 行，没有沿用 §2.4 第 2 步规定的 `user_delivery` duplicate 语义，导致首发失败或未投递时，重放仍回报投递成功（探针已复现）。返工只有 1 条，范围很小（见文末）。

## 一、逐条判定

| # | 条目 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | hold 回复放行时同事务闭合并 ack 原请求 | PASS | `_release_one_locked` 改为从 outbox 行取关联：intent=reply，payload 的 `replyTo` 优先，其次 `reply:` 幂等键，actor 取 sender。闭合 in 行、ack、清 `link_hold` 都在 `update_link_ledger` 的同一个事务内。探针重放了第一次验收的场景（`message.reply` Hold，再 `update_link_ledger(release=(reply_id,))`）：released 为 `(reply_id,)`，`pending_messages(ALPHA) == ()`，ledger 没有 reply 的 `out` 行，二次放行返回 `()`。L8b 断言 in 行 closed 且 `sample_pending=1` |
| 1b | 原请求已结算时走 `_fail_reply_locked` | PASS | L8c 与探针（先 ack 原请求，再放行）：`failed=(reply_id,)`，outbox 行进 DLQ，原因为 `MESSAGE_REPLY_UNAVAILABLE`。排干时 `_drain_one_locked` 会在同一事务内 ack inbox 行，因此仍会走 fail 分支并得到 `LINK_RECLAIMED`，与 B5 一致（读码） |
| 2a | `user:` 同键重放不再问 gate，W 满也返回原结果 | 计数 PASS | 探针：首发后把 gate 改为 W 满，同键重放得 `ok:true`、同一 messageId、`duplicate:true`；gate 1 次，`submitted` 1 次，`user_delivery.deliver` 1 次 |
| 2b | 重放识别的正确性（B6「并发同 ID」，§2.4 第 2 步） | FAIL | ledger `out` 行在投递前写入，它只说明「已登记」，不说明「已投递」。探针 R1（并发）：首发停在 `deliver` 内时发同键重放，重放立刻得 `accepted:true, duplicate:true`，随后首发失败，抛 `user delivery failed` 并释放该行。调用方收到的是成功，消息却从未送达。探针 R2（首发在投递前以 BaseException 中断，模拟进程崩溃）：行未释放，重放回报成功，`deliver` 调用 0 次。规格要求 `user:` 目标「沿用 `user_delivery` 的 `duplicate` 语义」，而 squire 的投递台账本身就持久保存原结果并去重 |
| 2c | 重放回执与原回执一致 | 偏离（并入返工） | 重放回执是合成的 `{accepted, queued:false, duplicate}`，缺原回执的 `receiverAdapter` 与 `nativeMessageId`。走 `user_delivery` 的 duplicate 结果即可带回原值 |
| 2d | 新读 `link_outbound_row` 的接线 | PASS | `InboxReadProjection.link_outbound_row` 为只读 SELECT，走独立读连接，与既有 `link_ledger_snapshot` 同一模式。facade 只转发，不经 actor 命令。生产 composition（`wiring/transport.py:588`）用的就是这个 facade。`tests/inbox_authority_exemptions.json` 在 `1484449bd..HEAD` 无改动 |
| 3a | `UserDeliveryTarget.parse` 失败释放 `out` 行 | PASS | W3 parse 测试；读码：parse 的 except 分支调用 `_link_user_release` |
| 3b | `deliver` 或 agent-bot 抛错释放 `out` 行 | PASS | W3 deliver 测试；探针：把 `_deliver_user_via_agent_bot` 换成抛错的替身后，无存活 `out` 行。`_user_delivery is None` 与 `accepted=false` 两条路径同样释放（探针） |
| 3c | 只为本节点 agent 写 `out` 行，且不破坏其他路径 | PASS | `_link_sender_books_ledger` 比对 `owner` 与 `node_id`。探针：本地 agent 发给 owner `user:op`，正常投递并写 1 行 `out`。外节点 agent 经本节点 `message.send` 在询问 gate 前就被拒为 `SENDER_UNRESOLVED`（W6 既有行为），所以不存在「远端 agent 经本节点 user: 发送」的路径，不受影响。adapter 发送者照常询问 gate，但不写 `out` 行 |
| 4 | 先红 | PASS | `b038630e0` 只改两个测试文件与红证据日志。scratch worktree（`git worktree add --detach`，已移除）中 `test_ledger.py`+`test_wiring.py` 为 7 failed / 25 passed，7 条都是行为断言失败（gate 抛 W 满、`assert not True`、`() == ('reply1',)` 等），不是收集错误。HEAD 为 32 passed |
| 5 | 无削弱断言 | PASS | `git diff 1484449bd..HEAD -- tests` 只有新增：两个文件，共 262 行新增，0 行删除。`_message` 加了可选参数 `idempotency_key`，没有改动任何既有断言 |
| 6a | 焦点集 links/inbox/delivery/messaging | PASS | 603 passed |
| 6b | `tests/daemon tests/shell tests/integration` | PASS（只有已知红与负载抖动） | 5 failed、8225 passed、6 skipped、6 xfailed。已知红 2 条：pi controlled_loader t04-06、login/device t11。另 3 条（`test_late_pid_refresh`、lark readiness 的 log flood、path_authz 的 hardlink）单独重跑 3 passed，属于负载抖动，与本包无关。其余已知红本次没有出现 |
| 6c | 结构门 | PASS | 恰好 1 条违规：`links/service.py` 2011 行（WP2-A，集成线已拆）。改动文件最大为 `read.py` 948 行 |
| 6d | 模块边界 / ruff | PASS | `check_module_boundaries` 退出 0；ruff 输出 All checks passed |
| 6e | `git diff --check 1484449bd HEAD` | PASS（附注） | `src`、`tests` 干净。`red-wp2b-rework.txt` 有 26 处 pytest 原样输出的行尾空白，证据 README 写的「git diff --check clean」不准确 |

## 二、探针（scratch，未提交；放入 tests 目录跑完已删除，`git status` 干净）

- P1 回复 Hold，放行：PASS（见 1）。
- P1' 先 ack 原请求，再放行：PASS，failed，进 DLQ。
- P1'' `message.send` 带 `replyTo` 发给 agent，Hold 后放行：released，原请求被 ack，与旧版靠 ledger 行的行为一致。
- P2 W 满后同键重放：gate 1、`submitted` 1、deliver 1，PASS。
- P2' 首发 `accepted=false` 后用同键重试：行已删除，重试重新询问 gate 并真实投递，没有被误判为重放，PASS。
- R1 并发同 ID：FAIL（见 2b）。
- R2 首发在投递前中断：FAIL（见 2b）。
- U1 带 `replyTo` 发给 `user:` 后同键重放：回复不写 `out` 行，所以重放会再次询问 gate、再次调用 `submitted`、再次 `deliver`（各 2 次）。按裁定 53 回复永不拒，`submitted` 按 §2.2 幂等，真实的 `user_delivery` 也按幂等键去重，因此实际危害小。但它与 B6「不重新授权」不一致，按下面返工的做法会一并解决。
- P3 owner 目标、外节点发送者、agent-bot 抛错、`_user_delivery=None`：PASS（见 3）。

## 三、接受的偏离

- 放行时改用 inbox 行 `consumed=0` 判断原请求是否仍待回复，不再以 ledger in 行闭合成功为准；in 行缺失只少一个样本。这符合「inbox 行是权威」的原则，可以接受。
- `_link_user_is_replay` 读失败时按「非重放」处理（再问 gate）。这是 fail-open 到旧行为，可以接受。
- W5 测试的替身 `_FakeUserDelivery` 不按幂等键去重。按下面的返工改用 duplicate 语义后，替身需要模拟按幂等键去重（这是加强替身，不是放宽断言），「deliveries 1」改为统计真实发出次数。

## 四、merge-tree

- `git merge-tree --write-tree refactor/org-structure-constrain HEAD`（集成头 `e523421db`，已含 WP2-A 的 `links/service/` 包）：结果 tree `b85c31ef3`，1 处冲突，在 `src/hyprial/daemon/impl/inbox/service/core.py::_insert_outbox`。与第一次验收相同：集成侧加 `summary`，本包加 `link_hold`，两块分别是列名行与值元组。`send.py`、`receive.py`、`retry.py`、`submit.py` 自动合并。`links/` 无冲突，本包没有改 `links/`。
- 新问题：合并后 `tests/daemon/links/service/` 有 8 个直接文件（`__init__`、gate、hold_release、ledger、reclaim、rework、samples、wiring），超过 7 个文件的结构门，CI 会硬失败。本分支单独看只有 7 个，所以证据 README 写的「no move needed」只在本分支成立。

## 五、集成者合并时要办

1. `core.py` 冲突：列名写成 `retry_started_at_ms, summary, link_hold`，值元组保留 `message.summary, link_hold`，`VALUES` 改为 15 个占位符（两侧各 14 个，自动合并后仍是 14 个，必须手改）。
2. `tests/daemon/links/service/` 拆目录，降到 7 个文件以内。例如把 `test_ledger.py`、`test_wiring.py` 移到 `tests/daemon/links/service/inbox/`（新目录要带 `__init__.py`）。
3. 第一次验收第七节的第 2 至 5 条仍然有效（`test_schema_migration` 的期望列表加 `link_hold`；三条 `already bound` 交互红；`outbox list` 的 `holdReason`，集成侧 `adee2b71f` 已在做；P9/P10 归 WP2-A）。
4. 证据日志的行尾空白：保持 WORM 原样，或在合并说明中注明。

## 六、返工清单

1. **`user:` 目标的重放识别改用 `user_delivery` 的持久 duplicate 语义（§2.4 第 2 步，B6），不再把「ledger 有 `out` 行」当成「已投递」。**
   - 识别为重放（可继续用 `link_outbound_row` 作为「不再问 gate」的信号）时：不问 gate，不再写 ledger 行，但仍走 `_deliver_user_via_agent_bot` 或 `user_delivery.deliver`。squire 的投递台账按 `idempotency_key=target_key` 返回原结果（带 `duplicate`、`nativeMessageId`）；首发没有真正投递时，这次会补投或得到真实失败。投递失败时照常释放 `out` 行并抛错。成功时 `submitted` 按 §2.2 幂等，可以照常调用。
   - 带 `replyTo` 的 `user:` 重放（U1）同样不得再问 gate。识别方式可以用 `user_delivery` 的 duplicate，也可以用原请求 in 行已闭合。
   - 补测（先红）：(a) 并发同 ID，首发停在 deliver 内，重放后首发失败，重放不得回报 `accepted:true`；(b) 首发登记后未投递就中断，重放必须真实投递 1 次；(c) 重放回执带原 `nativeMessageId`；(d) `user:` 回复重放 gate 调用 1 次。W5 的替身按幂等键去重，原有断言保持不变。
   - 只改 `inbox_surface/link_send.py`、`inbox_surface/send.py` 与测试、证据；不改 `links/`、`ports.py`、`queries.py`。
