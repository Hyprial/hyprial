# WP2-A 第三次验收：ACCEPTED

- 分支：`wp2a3/send-links`，worktree `/private/tmp/hf-wp2a3-send-links`，HEAD `8b6d61bde`（返工 3：`3b9dc8665` 测试、`cd3dd02cd` 修复、`8b6d61bde` 证据）。
- 上次验收：`d7a863c5a`（wp2a-acceptance-2.md，REJECTED，§6 三条返工）。集成分支 `refactor/org-structure-constrain` 在 `700c95a3c`（含 `75d29f6ad`）；`wp2b/send-links` 在 `c0260a5f6`；`wp2b3/send-links` 在 `a7454a635`。
- 验收者：opus 角色。所有命令由验收者用 worktree 自带 `.venv`（`uv run`）亲自执行。开工时 `git status` 干净。探针、scratch worktree 都在 scratchpad，已删除或未提交。

结论：§6 三条返工全部落实。暂存槽空窗已封住：Q2b 及三种镜像交错全部通过，持锁区内没有 store 或端口 I/O。裁定 70 的 settled 集合行为正确，且随 purge 截断，有界。证据更正准确。回归与各项门禁通过。合并文本层面无冲突，但与 WP2-B 合并后结构门会因 `tests/daemon/links/service` 有 8 个文件而变红，由集成者在合并时处理（见 §5）。

## 1. 逐条判定

| # | 条目 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1a | §6.1 暂存槽唯一入口（静态） | PASS | 本 actor 文件只有四处修改，都写 `st.pending_file`，且都在 `_mem` 内：`_mint_locked`（gate.py:491）、`_reclaim`（reclaim.py:316-325）、`_retract_single_sided`（holds.py:367-374，调用方 core.py:174-175 持 `_mem`）。另有 `_ensure_local_files` 只在没有文件、没有 intent、槽也为空时写 rev 0 空文件，不持锁。`store.write` 只剩两处调用：`_apply_pending_files` 和 `_ensure_local_files`，都不在锁内 |
| 1b | `_apply_pending_files` 不先清槽、CAS 清槽 | PASS | reclaim.py:60-76：在 `_mem` 内只读出 `file`；`store.write` 返回后，或抛 ValueError 后，在 `_mem` 内判断 `st.pending_file is file` 才置 None |
| 1c | 持锁区内无 store 或端口 I/O（锁探针） | PASS | 探针插件把 `_mem` 换成带线程计数的锁，记录持锁时对 `LinkStore.write/flush/refresh`、FakeInboxPort 六个方法、FakeOrgFs I/O、presence、pac 的任何调用。在 HEAD 上跑 `tests/daemon/links`：262 passed，违规 0。在 `d7a863c5a` 引擎上跑：违规 3，都是 `store.write @ _retract_single_sided:366`。说明探针有效，返工确实消除了这处违规 |
| 1d | Q2b 复跑和镜像交错 | PASS | 见 §2。在 HEAD 上连续跑 3 次，12/12 通过；在旧引擎上 11 failed |
| 2a | §6.2 settled 的填充 | PASS | `_reconcile`（reclaim.py:163-170）对 in 方向终态行写 `settled[id] = expires_at_ms`；`_rebuild`（reclaim.py:122-129）对 drained/closed/acked/expired 行同样写入。探针 `r70_rebuild_after_restart`：ledger 中预置 acked 行，`start()` 后 m1 在 settled 中，不在 admitted 和 pending_inbound 中 |
| 2b | `admit_dispatch` 对 settled id 的处理 | PASS | gate.py:136-141 在 `admitted` 短路之后返回 True，不写 `admitted` 或 `pending_inbound`。探针对 acked、expired、closed 各重复询问 5 次：id 不在 admitted 和 pending_inbound 中，`policy.unclosed == 0`，`top_fields(A)` 与询问前完全相同（没有在途计数）；再 tick 一次仍不回到 pending_inbound |
| 2c | drained 语义保持 | PASS（有一处行为变化，已接受） | 探针 `r70_drained_keeps_reclaimed_semantics`：drop 后 m1 的 ledger 状态为 `drained`；`settle_hint == "ack"`；回复 m1 时 `admit_send` 抛 `LINK_RECLAIMED`；`unclosed == 0`。变化：`admit_dispatch(m1)` 由旧引擎的 False 变为 True，见 §3 |
| 2d | 截断与有界 | PASS | reclaim.py:151-159 每次 reconcile 先按 `expiry > now - message_ttl_ms` 截断，horizon 与同一 tick 发给 ledger 的 `purge_before_ms` 相同。探针：1 万个 acked id（expires = now+1s），tick 后 settled 有 10000 个；时间推进到 expiry+ttl+5s，两次 tick 后 settled 为 0，ledger 中这些行也已被 purge。WP2-B 真实 ledger 的 purge 条件是 `expires_at_ms < cutoff`，与引擎侧 `> horizon` 只在恰好相等时差 1 ms，可以忽略 |
| 2e | 新 id 不混淆 | PASS | settled 以 `message_id` 为键，按 recipient actor 分开存。探针 `r70_new_id_still_counts`：m1 已 settled 后，新 id m2 准入，进入 pending_inbound，`unclosed == 1`。代码中没有任何路径把 settled 的 id 回写到 admitted 或 pending_inbound。ledger 的 drain/expire 都是终态，不会转回 open（WP2-B `_LIVE_INBOUND_STATES` 只做单向更新），所以同一 id 不会再次打开 |
| 3 | §6.3 证据更正 | PASS | wp2a.md §5.1 写明 `test_p3_a_drop_racing_a_flush_is_not_lost` 的红只来自返回形状断言，撤回断言没有执行到；§5.5 Contract-Impact 改为 `link.drop` 的返回形状对 CLI 可见（新状态 `reclaiming`、不再有计数），交 WP6/WP8。§6.2 对 `red-rework3.txt` 的陈述与原始日志一致：Q2b 失败在 `assert B not in entries`，A 文件是 `{b rev1, c rev2}`；两条 R70 都失败在 `'m1' not in {'m1'}`；3 failed, 7 deselected。§6.4 的数字（7 个文件、最大 595 行）验收者复核一致 |
| 4a | `tests/daemon/links` | PASS | 262 passed |
| 4b | 编号计数 | PASS（偏差已接受） | 实测为 70，不是 69。多出的一个是 `test_r70_`：裁定 70 用例的名字落进了 R 组的编号空间。证据 §6.3 已如实写明 70。见 §3 |
| 5 | 先红后绿 | PASS | `3b9dc8665` 只改 `tests/daemon/links/service/test_rework.py`（父提交 `d7a863c5a`）。在 scratchpad 中 `git worktree add --detach` 后执行 `-k 'q2b or r70'`：3 failed, 7 deselected，失败的三条与 red-rework3.txt 一致。worktree 已删除 |
| 6a | 焦点 5 目录 | PASS | 851 passed |
| 6b | `tests/daemon` 全量 | PASS（含已知红） | 6593 passed，3 failed（2 条已知红，1 条负载抖动，单独重跑通过），见 §4 |
| 6c | 结构门、模块边界门、ruff、空行 | PASS | 两个门都 exit 0，`violations: []`；`uv run ruff check .` 输出 All checks passed；`git diff --check d7a863c5a HEAD -- ':!docs/evidence'` 无输出，rc 0 |
| 6d | `links/service/` 规模 | PASS | 7 个文件（含 `__init__.py`），合计 2293 行，最大 595 行（core.py） |
| 6e | 受保护文件 | PASS | `git diff d7a863c5a HEAD -- links/{ports,model,controller,policy}.py` 为空；`store.py` 也没有改动 |
| 7 | 合并就绪 | PASS（合并后有结构门问题，交集成者） | 见 §5 |
| 8 | Contract-Impact（WP6/WP8） | 已报告 | 见 §6 |

## 2. 探针结果

探针文件：scratchpad `acc3/test_acc3_probes.py`（12 条）和 `acc3/lockprobe_plugin.py`，都没有提交。

- Q2b〔通过〕先入队 `drop(A,B)`，然后包装 `store.write`：遇到第一份 A 的、不含 B 的文件时阻塞。service 线程 tick，阻塞在 `_apply_pending_files` 的写入上。阻塞期间主线程调 `add(A,C)`。放行后再 tick 29 次。结果：A 的已提交文件不含 B、含 C；B 的文件不含 A；包装 `_commit` 记录到的 A、B 每一次提交，revision 都严格递增，没有同一 revision 先后提交两份不同内容；A 对 B 的 barrier 已清除，这一对没有卡住。原因：阻塞期间槽里仍是撤回后的文件，`_mint_locked` 以它为基础铸造，写入返回后 CAS 发现槽已换成新文件，因此不清槽，下一次 hand-over 把新文件交给 store。
- 镜像 1〔通过〕先 `add(A,C)`，阻塞 A 含 C 的那次写入。阻塞期间调 `drop(A,B)` 和 `add(A,D)`。结果：B 被撤回，C 保留，revision 严格递增。D 返回 `LINK_SELF_FULL`（k=2，drop 还在异步进行），这是 R10 规定的行为，不是缺陷。
- 镜像 2〔通过〕先 `add(A,D)`，阻塞 A 含 D 的那次写入。阻塞期间调 `drop(A,D)`，即撤回正在写入的同一个 peer。结果：D 不在 A 的最终文件中，B 保留，revision 单调。
- peer_withdrew 路径〔通过〕B 在自己的文件中撤回 A，A 回收时把撤回写入阻塞，阻塞期间调 `add(A,C)`。结果：B 不在 A 的文件中，C 在，revision 单调。
- 线程身份〔通过〕在另一个线程上调 `add`、`drop`、`admit_dispatch`，期间 `store.write` 没有被调用；之后 tick 中的每一次 `store.write` 都发生在 service 线程上，并且调用时 `_mem` 未被持有。
- 对照：同一组探针在 `d7a863c5a` 引擎上 11 failed、1 passed（Q2b、两个镜像、peer_withdrew 都失败，R70 各条也都失败）。
- 裁定 70：acked、expired、closed、新 id、rebuild、drained、1 万条截断，共 7 条，全部通过（见 §1）。

## 3. 接受的偏差

- 编号计数为 70：`test_r70_*` 与 R 组编号空间冲突。证据 §6.3 已如实说明，接受。建议日后改名为 `test_ruling70_*`，让计数回到 69，不作为返工项。
- drained id 的 `admit_dispatch` 由 False 变为 True（旧引擎返回 False，现在因 drained 也记入 settled 而返回 True）。这是按 acceptance-2 §6.2 的要求做的（settled 包含 drained）。WP2-B 真实 ledger 的 `_drain_one_locked` 在同一事务中置 drained 并 ack 收件行，所以 dispatch 不会把已排干的请求再投递出去。受影响的只有 read_body 回退对已排干行的询问：现在放行，并且不占 W，这正是裁定 70 要的效果。回复已排干请求时仍得到 `LINK_RECLAIMED`，`settle_hint` 仍是 `ack`。接受。
- `_reclaim` 在 `_mem` 内调 `store.intents()` 和 `store.index()`。这两个都只读内存（一次 dict 拷贝或一次引用读取），没有 I/O，不加锁，符合 §3.5。接受。

## 4. 测试运行

- 焦点 5 目录（links、inbox、dispatch、delivery、ipc）：PASS，851 passed，用时 62 s，heartbeat 比例的已知红这次没有出现。
- `tests/daemon` 全量：PASS（含已知红），6593 passed，3 failed，5 skipped，5 xfailed，1 xpassed，用时 1320 s。失败 1 是 pi `controlled_loader` t04-06，失败 2 是 ipc cost heartbeat，都是已知红。失败 3 是 `sessions/actors/test_session_persistence_overload.py::test_completed_agent_result_waits_for_real_persistence_capacity[128]`，不在已知红清单中；单独重跑该文件 6 passed。本分支相对 `d7a863c5a` 没有改动 sessions，判定为全量负载下的时序抖动，与本包无关。没有新增可复现的红。

## 5. 合并检查

- `git merge-tree --write-tree refactor/org-structure-constrain HEAD`（集成分支在 `700c95a3c`）：无冲突，树 `c210a9f3c`。
- `git merge-tree --write-tree wp2b/send-links HEAD`（`c0260a5f6`）：无冲突，树 `5f73a7ae4`。
- `git merge-tree --write-tree wp2b3/send-links HEAD`（`a7454a635`）：无冲突，树 `d0575d493`。合并树中旧的单文件 `links/service.py` 已被删除（wp2b3 自分叉点起没有改过它），只剩 `links/service/` 包。验收者把这棵树临时 checkout 到 scratch worktree（用后已删除）：
  - `tests/daemon/links`：311 passed。WP2-B 的 ledger、wiring、composition 测试与异步 drop 语义兼容。
  - 结构门 FAIL：`module-file-count`，`tests/daemon/links/service` 实际 8 个，上限 7。本分支在该目录有 6 个（`__init__` 加 5 个测试文件，`test_rework.py` 是返工 1 加的），WP2-B 另加 `test_ledger.py` 和 `test_wiring.py`。两边单独都合规，合并后超限。`wp2b/send-links` 的合并树同样是 8 个。

## 6. Contract-Impact（WP6/WP8）

- 代码确认：`views.py:203-218` 中 `drop` 返回 `{actor, peer, state: "reclaiming", written: false}`；如果没有本侧引用，返回 `state: "absent"`。计数（drained、released、cancelled）只出现在 `_reclaim` 发出的 `link.reclaimed` 事件里（reclaim.py:380-394，另含 `drainedMessageIds`、`reason`、`lifecycle`、`taskId`）。
- WP6 CLI（集成分支 `shell/impl/cli/commands/link/render.py:205-221`）可以容忍新形状：`_is_queued` 为假（没有 `queued: true`，也没有 holdReason）；`state` 不是 `reclaimed`，也没有整数 `drained`，所以落到最后一个分支，输出 `link A -> B: reclaiming`，不会报错；`--json` 原样透传。缺点是文案不再说「dropped」，也没有排干计数。需要后续跟进，不阻塞合并：WP6 为 `reclaiming` 补一句文案（例如「drop accepted; reclaim in progress」），`tests/shell/cli/links/test_cli_link.py:262` 中带 `drained: 2` 的夹具改为以事件为准；WP8 在 `contract/` 中登记新状态值 `reclaiming`，并说明计数已移到 `link.reclaimed` 事件。集成分支 `contract/` 中目前没有 `reclaiming`。

## 7. 集成者合并时要做

1. 合入 WP2-B（wp2b 或 wp2b3）前，先解决 `tests/daemon/links/service` 文件数超限（8 > 7）。例如把 `test_rework.py` 移到 `tests/daemon/links/service/rework/` 或按主题并入现有文件，或把 WP2-B 的 `test_ledger.py`、`test_wiring.py` 移到 `tests/daemon/links/composition/`。然后重跑结构门。
2. 合并后重跑 `tests/daemon/links`（验收者在 wp2b3 合并树上测得 311 passed）和焦点 5 目录。
3. 把 §6 的 Contract-Impact 转给 WP6（drop 文案、CLI 夹具）和 WP8（`contract/` 登记 `reclaiming`）。
4. 上次验收 §5 第 3 条仍然有效（`link_send` 先判 `governs`；G12 的 sink 那一半由 WP2-B 补；删除 E2E permissive gate）。
5. 可选：把 `test_r70_*` 改名为 `test_ruling70_*`，让编号计数回到 69。
6. 验收通过后，按 acceptance-2 的集成者补充把 `wp2/send-links` 快进到 `8b6d61bde`（或本验收提交）。
