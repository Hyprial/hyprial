# WP2-A 第二次验收：REJECTED

- 分支：`wp2/send-links`，worktree `/private/tmp/hf-wp2-send-links`，HEAD `3d41e7703`（返工 2：`a02b5c286` 测试、`7820af5cc` 修复、`3d41e7703` 证据）。
- 上次验收：`0c7c9baa3`（wp2a-acceptance-1.md，REJECTED）。集成分支 `refactor/org-structure-constrain` 在 `290f64e3a`；`wp2b/send-links` 在 `1484449bd`。
- 验收者：opus 角色。所有命令由验收者在本 worktree 用 `uv run`（worktree 自带 `.venv`）亲自执行。开工时 `git status` 干净。探针只放在 scratchpad，没有提交。

结论：P-1、P-4、H14、SA11、空行、编号计数、结构门、测试运行、合并全部通过。但有两处不通过。第一，P-3 只修了一半：IPC 线程不再写 store，`_commit` 也加了 CAS，但新增的暂存槽在 service 线程上「先清空、后写 store」，中间有空窗。在这个空窗里并发铸造，会让已 drop 的 peer 重新写回文件，并且永久留下。第二，裁定 70 未满足：对已结算的 id 再次调用 `admit_dispatch`，该 id 会重新进入 `pending_inbound`，`unclosed` 加 1。打回返工。

## 1. 逐条判定

| # | 条目 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1 | P-1 回复非 owner 用户 | PASS | `gate.py:77-82` 只在 `reply_to is None` 时拒绝。探针 q1：W 占满（`unclosed >= W`）后，`intent="reply"`、`reply_to="mu-req"` 发给 `user:p` 返回 `Allow(None)`；新请求发给 `user:p` 和 `user:q` 都抛 `LINK_WINDOW_FULL`；连续 5 次发给 owner `user:o` 都是 `Allow`。WP2-B 验收观察到的引擎侧拒绝已在 `service/gate.py` 修正 |
| 2a | P-3 IPC 只入队 | PASS | 静态检查：`add`、`drop`、`admit_send`（Mint/evict）、`bind_task` 只改 `pending_file` 和 `pending_reclaims` 再 `kick()`；`drop` 返回 `{state: reclaiming, written: false}`。探针 q2：主线程依次调用 `add`、`drop`、`admit_send`、`admit_dispatch`、`settle_hint`，期间 `store.write`、`inbox.apply`、`inbound_live`、`ledger_snapshot`、`_reclaim` 都没有被调用（线程身份断言）；之后由 service 线程 tick 完成回收 |
| 2b | P-3 `_commit` CAS | PASS（按身份比较） | `store.py:520-526`：只有 `_intents[actor] is file` 时才删除。按对象身份比较，不比较 revision；现在 `write` 和 `flush` 都只在 service 线程上运行，可以接受。上次验收的交错（flush 写到一半时并发 drop）由 `test_p3_a_drop_racing_a_flush_is_not_lost` 覆盖，PASS |
| 2c | P-3 暂存槽交错 | FAIL | 见 §2 Q2b。`_reclaim`（`reclaim.py:287-293`）和 `_apply_pending_files`（`reclaim.py:56-64`）都在 `_mem` 内把 `st.pending_file` 置为 None，然后在锁外调 `store.write`。如果 IPC 线程的 `_mint_locked` 恰好落在这段空窗里，它会以 `store.intents()` 或已提交文件为基础构建新文件，而基础里还含有被撤回的 peer。下一次 pass 用同一个 revision 把它提交回去。已 drop 的 B 因此永久留在 A 的文件里，A–B 的屏障永远清不掉 |
| 3 | P-4 `_seen` | PASS | `_seen` 的每一处修改（`core.py:232-239`）都在 `_mem` 内；`_n_active` 的三个调用点（`core.py:199/256`，经 `gate.py:286`、`views.py:239`）也都在 `_mem` 内。探针 q3：3 个线程运行，20000 轮 `_refresh_seen`（每轮 2 个新 agent），并发调用 `admit_dispatch`、`stats`、`list_links`、`top_fields`，`setswitchinterval(1e-6)`，没有抛错 |
| 4a | H14 | PASS | `test_h14_a_row_expiring_inside_the_pass_is_only_expired`：`apply` 前把 held 行的过期时间改为 now，断言 update 里有 release，但结果只在 `expired` 里，`outbound == []`，`outboundInFlight == 0`。场景有意义。它在修复前的代码上也通过（属于补测用例，不是缺陷） |
| 4b | SA11 | PASS | 新测试断言 `grew >= 1`（增窗分支至少进入一次，每次增量 ≤1 且 grad ≤0）、`fell >= 1`，最终 `W > k`。断言本身保证场景不再空转。`red-sa11-vacuous.txt` 记录了旧场景全程 `W=k=2`、`increases: 0`，与上次 P-7 一致 |
| 5 | 裁定 70 | FAIL | 探针 q5：`admit_dispatch(A, B, m1)` 准入后，ledger 行改为 `acked`，tick 后 `unclosed == 0`；再次调用 `admit_dispatch(A, B, m1)`，结果是 `pending_inbound` 含 m1，`unclosed == 1`。q5b 用 `expired` 状态复现。根因：`_reconcile` 把已结算的 id 从 `admitted` 和 `pending_inbound` 移除，却没有记录「已结算」；`gate.py:131-151` 的 `admitted` 短路因此失效，请求被重新计入 |
| 6a | 行尾空行 | PASS | `git diff --check 0e552780d HEAD -- ':!docs/evidence'` 无输出，rc 0 |
| 6b | 编号计数 | PASS | `grep -rhoE 'def test_(st\|g\|h\|r\|sa)[0-9]+_' tests/daemon/links \| sort -u \| wc -l` = 69 |
| 6c | 红日志 | PASS（有一处陈述不准） | `red-rework2.txt` 是 pytest 原始输出，11 failed / 47 passed。证据 §5.1 说 `test_p3_a_drop_racing_a_flush_is_not_lost` 的红「证明撤回丢失」，实测它的红只来自返回形状断言（`'reclaimed' == 'reclaiming'`），没有执行到撤回断言 |
| 7 | 先红后绿 | PASS | `a02b5c286` 只改测试（父提交 `0c7c9baa3`）。在 scratch worktree（`git worktree add --detach`，用后已删除）跑 `tests/daemon/links`：11 failed，248 passed。失败的 11 条与红日志一致：P-1 抛 `window W is full`；P-3 commit 为 `intents() == {}`；P-3 mint 丢了 D；P-4 抛 `RuntimeError('dictionary changed size during iteration')`；另外 5 条（H1、H7、R2、R10、R11）是返回形状或时序断言 |
| 8a | `tests/daemon/links` | PASS | 259 passed |
| 8b | 焦点 5 目录 | PASS | links + inbox + dispatch + delivery + ipc：848 passed（这次 heartbeat 比例的已知红没有出现） |
| 8c | `tests/daemon` 全量 | PASS（含已知红） | 6591 passed，2 failed，5 skipped，5 xfailed，1 xpassed，用时 1051 s。失败的是 pi `controlled_loader` t04-06 与 ipc cost heartbeat，都是已知红；没有新增红 |
| 8d | 结构门、模块边界门、ruff | PASS | 两个门都 exit 0，`violations: []`；`uv run ruff check .` 输出 All checks passed |
| 9 | 合并就绪 | PASS | 见 §4 |

## 2. 探针结果

探针文件：scratchpad `probes/test_acc2_probes.py`，没有提交。连续跑 4 次，每次都是 3 failed、6 passed，结果稳定。

- Q1〔通过〕P-1：见上表。
- Q2〔通过〕线程身份：IPC 入口不触达 store 写入、inbox I/O，也不调用 `_reclaim`。
- Q2b〔缺陷，严重〕暂存槽空窗导致撤回丢失。复现步骤：
  1. A–B 已成立，调 `drop(A, B)`（入队）。
  2. 把 `store.write` 包一层：遇到第一份「A 的、不含 B」的文件时阻塞。
  3. service 线程 tick，阻塞在 `_reclaim` 的 `store.write` 上。这时 `pending_file` 已被置为 None，撤回的文件还没进 store。
  4. 主线程调 `add(A, C)`。`_mint_locked` 以 committed `{B}` 为基础，暂存 `{B, C}`（rev 2）。
  5. 放行阻塞，再跑 28 次 tick。

  结果：A 的已提交文件为 `{B, C}`，rev 2；B 的文件为 `{}`，rev 2；A 和 B 的 `reclaimed` 中都含对方；`list_links(A)` 中 B 显示为 `reclaimed`。B 先看到了 rev 2 的 `{}`，按 `peer_withdrew` 回收了 A；随后 A 又提交了内容不同的 rev 2，B 被写回 A 的文件。`_clear_barriers` 要求 A 的文件不再含 B，这个条件永远不成立，所以这一对永久卡住。同一 revision 先后提交两份内容不同的文件，违反了单调修订号。对照组（drop 后先 tick，再 add）通过，说明问题只在交错时出现。`_retract_single_sided`（`holds.py:364-366`）有同类问题：它不看 `pending_file`，而且在持 `_mem` 时调 `store.write`，违反 §3.5 的「持锁区内不调 store」。好在它每次 pass 都会重评，能自愈，不单独判失败。
- Q2c〔通过〕`_apply_pending_files` 空窗里的并发铸造：经过几次 pass 后，C 和 D 都在 A 的文件里。C 是本节点 agent，靠对端互引重新铸造自愈；同一 revision 被重复使用的问题与 Q2b 相同。
- Q3〔通过〕`_seen` 压力测试，见上表。
- Q5、Q5b〔缺陷〕裁定 70，见上表。

## 3. 接受的偏差

- `drop` 改为返回 `{actor, peer, state: "reclaiming", written: false}`，不再同步返回 drained/released/cancelled 计数（§2.5 原写 `state: reclaimed` 加计数）。这是按 §3.5 和上次返工第 2 条的必然结果，计数改由 `link.reclaimed` 事件给出。接受。但这个形状经 IPC `link.drop` 暴露给 WP6 CLI，证据里写的「internal」不准确，见 §5。
- `_commit` 的 CAS 按对象身份比较，不比较 revision。接受（理由见上表 2b）。
- G15 把 `len(calls) == 1` 放宽为 `>= 1`，因为 drop 改为异步后回调由 pass 触发。接受。
- H14 在旧代码上也是绿：它是补测用例，不是缺陷回归。接受。

## 4. 合并检查

- `git merge-tree --write-tree refactor/org-structure-constrain HEAD`（集成分支在 `290f64e3a`）：无冲突，树 `42992c7b2`。
- `git merge-tree --write-tree wp2b/send-links HEAD`（wp2b 在 `1484449bd`）：无冲突，树 `72bbf1704`。wp2b 没有改 `links/` 下的产品文件。但 wp2b 分支上的旧测试副本（`test_hold_release.py:145/373`、`test_reclaim.py:104/283`）断言的是同步语义（`store.intents()[A]`、`outcome["drained"]`）。合并时三方合并会取本分支版本，属于文本层面不冲突；合并后应重跑一遍确认。

## 5. 集成者合并时要做

1. 合入 WP2-B 后重跑 `tests/daemon/links` 和 WP2-B 的 L/W 组，确认没有测试依赖 `drop` 的同步计数或 `store.intents()` 的同步写入。
2. WP6 `render_link_change`（`shell/impl/cli/commands/link/render.py:205-221`）目前把 `state: reclaiming` 渲染为 `link A -> B: reclaiming`，不报计数。要么请 WP6 补一行「drop accepted」文案，要么由 WP8 在 `contract/` 记下 `reclaiming` 这个新状态值（Contract-Impact）。
3. 上次验收 §5 的第 2、3 条仍然有效（`link_send` 先判 `governs`；G12 的 sink 那一半由 WP2-B 补；删除 E2E permissive gate）。

## 6. 返工清单

1. P-3 暂存槽空窗（Q2b）。所有对本 actor 文件的修改都只经过 `st.pending_file` 这一个槽：
   - `_reclaim` 和 `_retract_single_sided` 都在 `_mem` 内，以 `pending_file or intents or committed` 为基础算出撤回后的文件，再赋给 `st.pending_file`，不直接调 `store.write`。
   - `_apply_pending_files` 交给 store 时不先清空槽。`store.write` 返回后，在 `_mem` 内判断：只有 `st.pending_file is file` 时才置为 None（对暂存槽做 CAS）。
   - 也可以用别的办法，只要 `_mint_locked` 的基础永远包含正在交给 store 的那份文件。

   增加确定性测试：阻塞撤回文件的 `store.write`，在阻塞期间调 `add(A, C)`；放行后跑几次 tick，断言 A 的已提交文件不含 B、含 C，并且所有提交过的同一 actor 文件 revision 严格递增。可以改写本报告的 Q2b 探针。先红再绿。
2. 裁定 70（Q5、Q5b）。在 `_reconcile` 和 `_rebuild` 中，把状态为 closed、acked、expired 或 drained 的入站 id 记入每个 actor 的有界集合 `settled`（随 ledger purge 截断）。`admit_dispatch` 遇到 `settled` 中的 id 时直接返回 True，不进 `admitted`、`pending_inbound`，不占 W，不计入在途（read_body 回退读的是已结算行）。drained 的 id 保持现有语义。增加测试：acked 和 expired 各一条，reconcile 后再次 `admit_dispatch`，断言 `unclosed` 仍为 0、id 不在 `pending_inbound`。先红再绿。
3. 证据修正：wp2a.md §5.1 关于 `test_p3_a_drop_racing_a_flush_is_not_lost` 的红，写明它只在返回形状断言处失败；§5.5 的 Contract-Impact 改为说明 `link.drop` 的返回形状对 CLI 可见（新状态值 `reclaiming`，不再有计数），交 WP6/WP8 知悉。

## 集成者补充（2026-10-15 09:4x）：返工 3 在分支 wp2a3/send-links 执行

上一个 deepseek worker 仍挂在 wp2 worktree 未 complete，为避免同树冲突，返工 3 在自 wp2 HEAD（f5a31d6c8）派生的 wp2a3/send-links 上做；完成验收后集成者把 wp2 快进到它。§6 的 3 条逐条必做；范围仍是 `links/store.py`、`links/service/`、`tests/daemon/links/`、`tests/support/links/`、证据目录；不改 ports/model/controller/policy。`link.drop` 的 `reclaiming` 返回形状保留（CLI 可见），由 WP6/WP8 知悉。
