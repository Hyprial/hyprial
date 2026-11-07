# WP2-B1 / WP2-B2 第三次验收：ACCEPTED

- 分支：`wp2b/send-links`，worktree `/private/tmp/hf-wp2b-send-links`，HEAD `2b6ee1f9f`
- 返工区间：`41780821e..2b6ee1f9f`（`13be4be0c` 先红测试，`300b6ed60` 修复，`2b6ee1f9f` 证据）
- 依据：wp2b-acceptance-2 第六节唯一返工条；wp2.md §2.2、§2.4 第 2 与第 6 步、§9.3 至 §9.6、B6；README 裁定 37/39/46/53/54/55/70
- 验收人：opus 验收角色，2026-10-15
- 结论：ACCEPTED。返工条的各项要求均已满足：第二次验收的两个探针（R1 并发、R2 中断）在 HEAD 行为正确，W 满重放、`nativeMessageId`、`user:` 回复重放全部达标。另发现 3 处只在重放失败路径上出现的小缺陷，都不会造成虚假成功或重复投递，列为合并后跟进（第六节），不阻断本次合入。

## 一、逐条判定

| # | 条目 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | `user:` 重放经 `user_delivery` 的 durable duplicate 结果识别，不以 ledger 行存在为准 | PASS | `send.py` 删除了合成回执分支。`link_plan.replay` 现在只表示「跳过 gate、不写第二行」，之后照常走 `_deliver_user_via_agent_bot` 或 `user_delivery.deliver`。`_link_user_sent` 只在 `outcome.duplicate` 为假时调用 |
| 2 | gate 只问 1 次 | PASS | 探针 P3：首发后 gate 改为 W 满，同键重放 `ok:true`，`admit_calls` 为 1 |
| 3 | `submitted` 只调 1 次 | PASS | P3 与 P2 的 `submitted_calls` 都是 1。duplicate 结果跳过 `_link_user_sent` |
| 4 | W 满时 deliveries 仍为 1 | PASS | P3：真实发出 1 次（`sent` 为 1），重放的 delivery 带 `duplicate:true`，ledger `out` 行保持 1 条 |
| 5 | 保留 `nativeMessageId` | PASS | P3：重放的 `nativeMessageId`、`messageId` 与首发回执相同，并带 `receiverAdapter:true`。新测试 `test_w5_replay_carries_the_original_native_message_id` 覆盖 |
| 6 | 重放的 `user:` 回复只问 gate 1 次 | PASS（有前提） | 以原请求 in 行 `closed` 识别。P4b 用生产注入路径 `fetch_pending` 写 in 行（不是手插），首发加重放共问 gate 1 次，`submitted` 1 次。没有 in 行时见第二节 (b) |
| 7 | 首发仍在进行时，同键并发重试不得先报成功 | PASS | 用新测试与仿真实 actor 的替身（P1）验证：首发停在 deliver 内时，重放在 0.5 s 内没有返回。首发失败后，重放得到失败（不是 `accepted:true`），`sent=0`、`submitted=0`、无残留 `out` 行。错误类型有瑕疵，见第六节第 1 条 |
| 8 | 首发中断（ledger 行已写、未投递）后重放会真实投递 | PASS | P2：首发以 BaseException 中断后，ledger 留 1 条 `out` 行。重放在 W 满时仍得 `accepted:true`：真实投递 1 次，gate 1 次，`submitted` 1 次，`out` 行仍为 `open` |
| 9 | 第一次验收返工第 1 条（hold 回复放行时闭合并 ack） | PASS（未回退） | `git diff 41780821e..HEAD -- src/hyprial/daemon/impl/inbox` 为空。L8 与 hold 相关测试 12 passed |
| 10 | 第一次验收返工第 3 条（parse 或 deliver 失败释放 `out` 行） | PASS（未回退） | `send.py` 中 parse、agent-bot 抛错、deliver 抛错、`accepted=false`、`_user_delivery is None` 五条释放路径都在。W3 测试通过 |
| 11 | 先红 | PASS | `13be4be0c` 只改 `test_wiring.py` 与红证据日志。scratch worktree（`git worktree add --detach 41780821e`，检出该提交的测试文件，跑完已移除）：4 failed / 16 passed。4 条都是行为断言失败（`assert not [...]` 先报成功、`0 == 1` 未投递、`KeyError: 'nativeMessageId'`、`window W is full`）。HEAD 上 20 passed |
| 12 | 无削弱断言 | PASS | `git diff 41780821e..HEAD -- tests`：只改了 `_FakeUserDelivery`（加强为按键去重），新增 4 条测试与 import，既有断言一行未改 |
| 13 | 测试替身与真实 `user_delivery` 的一致性 | PASS（附注） | 见第二节第 3 条 |
| 14a | 焦点集 links/inbox/delivery/messaging | PASS | 607 passed |
| 14b | `tests/daemon tests/shell` | PASS（只有已知红） | 4 failed、8131 passed、6 skipped、5 xfailed、1 xpassed（25 分 53 秒）。4 条全是已知红：pi controlled_loader t04-06、ipc cost heartbeat、shell CLI command inventory、login/device t11。其余已知红本次没有出现 |
| 14c | 结构门 | PASS | 只有 1 条违规：`links/service.py` 2011 行（WP2-A，集成线已拆）。`send.py` 926 行，`link_send.py` 527 行，`test_wiring.py` 849 行 |
| 14d | 模块边界 / ruff / `git diff --check 41780821e HEAD` | PASS | 边界检查退出 0；ruff 输出 All checks passed；`diff --check` 无输出。第二次验收指出的 `red-wp2b-rework.txt` 行尾空白已去除（只删 pytest 的填充空格，内容不变） |

## 二、对 worker 所述限制与替身的判断

(a) 「首发已投递，但在 `submitted` 前崩溃，重放时不会再通知 `submitted`」：接受。ledger 的 `out` 行在投递前持久写入，崩溃后不会丢。`service.start()` 从 ledger 快照重建 W 与在途，所以 W 的计数是准的。丢掉的只是内存中 `submitted` 那一刻的发出时刻样本。按 §2.2（observer 只改内存、按 id 幂等）与裁定 46（observer 是样本来源，W 的精确计数靠 ledger 对账），这属于可观测性损失，不影响正确性。

(b) 「回复重放依赖原请求的 in 行，没有时会再问 gate」：接受。
  - 生产中 in 行是怎么来的：证据 README 说「由 link service 在注入时写」，不准确。实际是 inbox 自己在注入事务里写：`projection.py` 的 `fetch_pending` 与 `fetch_pending_admitted` 调用 `_write_inbound_ledger_locked`，事务提交后才通知 `LinkLedgerSink.injected`。agent 经正常注入拿到的请求一定有 in 行（P4b 实测）。只有绕过注入、直接拿到请求 id 的情况才没有。
  - 没有 in 行时会不会重复占 W（P4 实测）：gate 问了 2 次，但 `submitted` 只有 1 次，真实投递 1 次，没有 `out` 行。真实 gate 的 `admit_send` 只在 `reply_to is None` 时追加预留，回复不预留。duplicate 结果也不会再调 `submitted`。所以不会重复占 W。裁定 70 针对的是 `admit_dispatch` 对已结算 id 的询问，和这里无关。
  - 剩余风险：重放时再问 gate，若原请求已从 inbox 清除，可能拿到 `MESSAGE_REPLY_UNAVAILABLE`。这是对一次已成功投递的重试报错，不会重复投递，也不会多计数。本分支的旧 gate（`links/service.py`）对非 owner `user:` 的回复也做 W 检查，集成侧拆出的 `links/service/gate.py` 已改为只限新请求（`reply_to is None`）。因此合并后，W 满不会挡住回复重放。

替身 `_FakeUserDelivery`：已对照真实实现（`squire/actors/delivery.py` 的 `UserDeliveryActor` 与 `squire/addressing.py` 的 `UserDeliveryLedger`/`ReceiverUserDelivery`）。
  - 成功路径一致：同一 `idempotency_key` 返回原结果，带 `duplicate=True` 与原 `native_message_id`。并发时同键按 key 串行。
  - 失败路径与真实不同：
    - 真实 ledger 会记录「不确定失败」（`definitely_not_sent=False`），之后同键请求得到 `duplicate` 的失败结果；只有 `definitely_not_sent` 的拒绝不记录。替身对任何失败都不记录。
    - 真实 actor 让跟随者加入在途的领头请求，领头失败时跟随者拿到同一失败结果（带 `duplicate`）。替身则让跟随者在领头失败后自己重新投递。
  - 结论：`test_w5_replay_during_a_still_running_first_send_does_not_claim_success` 的前半段（首发未完成时不报成功）不是同义反复，在真实语义下同样成立。它的后半段（重放随后自己投递成功）描述的是 `definitely_not_sent` 那一种失败的行为，不是一般的投递失败。我另写了一个仿真实替身做探针 P1（记录不确定失败，跟随者加入领头），确认被测代码在真实语义下也不会虚报成功。替身对齐列为跟进（第六节第 3 条）。

## 三、探针（scratch，未提交；放入 tests 子目录跑 5 遍，结果稳定，跑完已删除，`git status` 干净）

- P1 并发同键，仿真实替身：首发停在投递内并最终失败。重放没有提前返回，最终也是失败，没有残留 `out` 行，`submitted` 为 0。重放抛出的是 `ValueError('duplicate synchronous correlation: link:release:<sender>:<id>')`，不是投递失败码（第六节第 1 条）。
- P1b 并发同键，用分支内的替身（首发失败后重放自己投递成功）：`submitted` 1、真实投递 1，但 `out` 行为 0。首发失败时已删掉该行，而重放按「重放」分支不再写行。结果是一条已投递的请求在 durable ledger 中没有 `out` 行（第六节第 2 条）。
- P2 首发中断后重放：PASS（见第一节第 8 条）。
- P3 W 满重放：PASS（gate 1、`submitted` 1、投递 1，`nativeMessageId` 相同）。
- P4 没有 in 行的 `user:` 回复重放：gate 2、`submitted` 1、投递 1、无 `out` 行，不重复计 W。
- P4b 经 `fetch_pending` 写入 in 行后的回复重放：gate 1、`submitted` 1。
- P5 首发成功后，重放遇到传输异常：执行 `except` 分支中的 `_link_user_release`，删掉了首发已投递请求的 `out` 行（之前 1 条，之后 0 条）。这是按第二次验收「投递失败时照常释放」的字面要求实现的结果（第六节第 2 条）。

## 四、接受的偏离

- 回复重放以原请求 in 行 `closed` 识别，而不是用 `user_delivery` 的 duplicate 结果在问 gate 之前识别。第二次验收已允许这两种方式任选其一。
- `_link_user_is_replay` 读失败时按非重放处理，即 fail-open 到再问 gate。与第二次验收已接受的处理一致。
- 去掉 `red-wp2b-rework.txt` 中 pytest 的行尾填充空格。严格说这改动了 WORM 证据。它只删空白，并在证据 README 里写明，可以接受；集成者在合并说明中注明即可。

## 五、merge-tree

- `git merge-tree --write-tree wp2b-intg/send-links HEAD`（集成者对账分支头 `fea7aeae6`）：无冲突，结果 tree `743e8ce56`。在 scratch worktree 中用该 tree 生成临时合并提交跑焦点集：657 passed。结构门只剩 `links/service.py` 那 1 条违规（集成分支中该文件仍在），ruff 干净。`tests/daemon/links/service/` 合并后为 7 个直接文件，集成侧 `30d226caf` 已把 ledger 测试移出。临时 worktree 已移除。
- `git merge-tree --write-tree refactor/org-structure-constrain HEAD`（集成头 `6619a919d`）：结果 tree `0bc36b48e`，1 处冲突，在 `src/hyprial/daemon/impl/inbox/service/core.py::_insert_outbox`，与前两次验收相同。冲突有两块：列名行（集成侧 `retry_started_at_ms, summary`，本包 `retry_started_at_ms, link_hold`），值元组末项（集成侧 `message.summary,`，本包 `link_hold,`）。`send.py`、`submit.py`、`receive.py`、`retry.py` 自动合并。

## 六、集成者合并时要办

1. 若直接对 `refactor/org-structure-constrain` 合并（而不是经 `wp2b-intg/send-links`）：`core.py` 列名写成 `retry_started_at_ms, summary, link_hold`，值元组保留 `message.summary, link_hold`，`VALUES` 改为 15 个占位符（自动合并后是 14 个，必须手改）。经 `wp2b-intg/send-links` 合并时，这一处已在该分支解决。
2. 第一次、第二次验收「集成者要办」中尚未完成的条目仍然有效（`test_schema_migration` 期望列、三条 `already bound` 交互红、P9/P10 归 WP2-A）。
3. 在合并说明中注明 `red-wp2b-rework.txt` 去掉了行尾空白。

跟进项（不阻断本次合入，建议在 B3 或单独小单中处理，都只涉及 `inbox_surface/link_send.py`、`send.py` 与测试）：

1. 重放失败路径的释放 correlation 冲突：`_link_user_release` 的 correlation id `link:release:<sender>:<id>` 是确定值。首发与跟随的重放同时收到同一失败结果时，两个释放并发进入 inbox actor，其中一个抛 `ValueError('duplicate synchronous correlation')`。它会盖掉原本的 `USER_DELIVERY_FAILED`，调用方看到的是内部错误。结果仍是失败，状态也一致，但错误码不对。修法：释放时容忍这个重复（捕获后视为已释放），或给 correlation 加唯一后缀。补一条用仿真实替身的测试。
2. 重放路径的 `out` 行维护：
   - P5：首发已投递，重放遇传输错误时删掉了首发的 `out` 行。
   - P1b：首发失败且已删行，重放随后真实投递，却不补写 `out` 行。
   - 后果：durable ledger 少计 1 个 W 名额。重启或对账后会多放行 1 个，有上界、可自愈，不会造成虚假成功或重复投递。
   - 建议：重放（`link_plan.replay`）失败时不释放行（交给 TTL 或对账）；重放得到非 duplicate 的成功时，以幂等方式补写 `out` 行（`open_outbound` 用 INSERT OR IGNORE 语义）。
3. `_FakeUserDelivery` 对齐真实失败语义：记录不确定失败，并让跟随者继承领头的结果。同时把 `test_w5_replay_during_a_still_running_first_send_does_not_claim_success` 的后半段改为真实语义下的期望（重放得到失败），或拆成 `definitely_not_sent` 与不确定失败两条测试。
4. 证据 README 中「in 行由 link service 在注入时写」的说法改为「由 inbox 注入事务写（`fetch_pending`/`fetch_pending_admitted`），之后通知 `LinkLedgerSink.injected`」。
