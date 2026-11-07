# WP2-A 第一次验收：REJECTED

- 分支：`wp2/send-links`，worktree `/private/tmp/hf-wp2-send-links`，HEAD `9f4eae902`。
- 集成基线：merge-base `a4900d08f`；集成分支 `refactor/org-structure-constrain` 在 `aab662594`。返工基线 `0e552780d`。
- 范围：wp2.md §9.5 WP2-A（`links/store.py` 与 `links/service/` 引擎）。接线、ledger、发送边界归 WP2-B，不在本次验收。
- 验收者：opus 角色。所有命令由验收者在本 worktree 用 `uv run`（worktree 自带 `.venv`）亲自执行。开工前 `git status` 无已修改的受跟踪文件。

结论：结构、缝、测试运行、合并全部通过。但探针在引擎里发现三处行为缺陷，其中一处会让 drop 或淘汰的撤回永久丢失。另有一条编号缺失，证据中两处陈述与实测不符。打回返工，返工清单见末节。

## 1. 逐条判定

| # | 条目 | 判定 | 证据 |
| --- | --- | --- | --- |
| 1a | 改动范围 | PASS | `git diff a4900d08f HEAD --stat`：src 只有 `links/policy.py`（+5）、`links/service/` 7 个文件、`links/store.py`；tests 只有 `tests/daemon/links/**` 与 `tests/support/links/{ledger,org}.py`；docs 只有 wp2.md 与 wp2 证据目录 |
| 1b | 缝不动 | PASS | `ports.py`、`model.py`、`controller.py`、`hooks/service.py`、`dispatch/admission.py`、`transport.py`、`application/__init__.py` 对 `a4900d08f` 与对 `aab662594` 的 diff 均为空 |
| 1c | policy 只改裁定 44 | PASS | `policy.governed` 只加 `user:` 目标返回 False 一处（5 行）；`test_policy.py` 把 p3 改为 p1 的 `user:` 豁免测试 |
| 1d | 文件数与行数 | PASS | `links/` 顶层 6 个 .py 加 `service/` 目录；`service/` 7 个文件，最大 `core.py` 589 行；`from hyprial.daemon.impl.links.service import LinkService` 可用（全套测试经此导入） |
| 2 | 语义探针 | FAIL | 6 条探针中 4 条失败，见 §2。P-1、P-3、P-4 是缺陷；P-2 记为观察，不判失败 |
| 3 | 先红后绿 | PASS（弱红） | 在 scratch worktree 中，用 `aef0f729f` 的 262 行骨架 `service.py` 替换 `service/` 包后跑 `test_gate.py`：21 failed，21 条均为 `E NotImplementedError`，与 `red-g-skeleton.txt` 一致。G/H/R 的测试与实现在同一批提交里落地，红日志是事后回放；SA 组的 `51087f0e2`（只含测试）先于 `e341edccb`（修复）。骨架红只证明「未实现」，强度不如行为断言红。最终测试断言的是行为，按 §9.5 第 3 条接受 |
| 4a | `tests/daemon/links` | PASS | `PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links -q`：252 passed |
| 4b | 焦点 5 目录 | PASS（含已知红） | links+inbox+ipc+delivery+dispatch：840 passed，1 failed。失败的是 `ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load`，属 dev 已知红 |
| 4c | `tests/daemon` 全量 | PASS（含已知红） | 6584 passed，2 failed，5 skipped，5 xfailed，1 xpassed，用时 1061 s。2 条失败是 pi `controlled_loader` t04-06 与 ipc cost heartbeat，均为 dev 已知红；无新增红 |
| 4d | 结构门、模块边界门 | PASS | `check_structural_limits.py` 与 `check_module_boundaries.py` 均 exit 0，`violations: []` |
| 4e | ruff | PASS | `uv run ruff check .`：All checks passed |
| 4f | `git diff --check` | FAIL | `git diff --check 0e552780d HEAD -- ':!docs/evidence'` 报 6 处 `new blank line at EOF`：`service/{_base,core,gate,holds,reclaim,views}.py`。红日志里的行尾空白是 pytest 原样输出，可以接受。证据 wp2a.md 写的「`git diff --check` clean」与实测不符 |
| 5 | 编号覆盖 | FAIL | `grep -rhoE 'def test_(st\|g\|h\|r\|sa)[0-9]+_' tests/daemon/links \| sort -u \| wc -l` 为 68，应为 69。缺 **H14**（放行与过期竞争：本次 pass 中恰好到期的行只出现在 `expired`）。证据 wp2a.md 写的「69 engine cases」与实测不符。S-B1、S-B2 共 3 个函数，存在 |
| 6 | 合并就绪 | PASS | `git merge-tree --write-tree refactor/org-structure-constrain HEAD` 无冲突（树 `879c0023e`）；`git merge-tree --write-tree wp2b/send-links HEAD`（wp2b 在 `86a02f5c7`）无冲突（树 `31954a9e3`），合并后 `links/service.py` 被包取代，wp2b 未改 `links/` |
| 7 | Contract-Impact | PASS | `git diff a4900d08f HEAD -- contract/` 为空。`link.minted`、`link.reclaimed` 取自 `ports.EVENT_LINK_*`（WP0），没有新增已发布字段。`link.*_failed` 只是日志事件名，不是 hook 事件 |
| — | 无旁路状态文件（§5 第 12 条） | PASS | `git grep -n 'LinkStateFile\|state\.json' -- src/hyprial/daemon/impl/links` 无匹配 |
| — | deque 切片修复（e341edccb） | PASS | SA12 是回归测试，`red-sa12-slice-trim.txt` 有修复前的 `TypeError`；`grep -rnE 'del [a-z_.]+\[[^]]*:[^]]*\]' src/hyprial/daemon/impl/links` 无匹配 |
| — | 样本不走 bus（裁定 46） | PASS | `observe_hook` 只调 `kick()`；样本只来自 `submitted`、`reply_received`、`injected`、`admit_dispatch`、tick 中的 ledger 对账 |

## 2. 探针结果

探针文件只放在 scratchpad，没有提交。

- P-1〔缺陷〕W 满时，回复非 owner `user:` 被拒。复现：U 向 A 发请求 `mu-req`（`injected`）。A 向 U 发 W 条请求，把 W 占满。然后 A 回复 U：`admit_send(target=U, intent="reply", reply_to="mu-req")` 抛 `LINK_WINDOW_FULL`（`service/gate.py:80`）。规格 §2.2 步骤 1.4 要求：回复校验通过后，`user:` 目标返回 `Allow`。裁定 53 只约束「发新请求」。ports §4.5 的原则是：回复用来闭合开放事项，不受 W 约束，否则 W 满的节点会死锁。现在的写法让 A 无法闭合 U 的请求，W 也就无法回落。G2 只测了新请求，所以没有发现。
- P-2〔观察〕内部 actor 作为发送方不豁免。`admit_send(sender=COORD, target=B)` 在 COORD 被视为本节点 agent 时返回 `Mint(B)`；COORD 不是本节点 agent 时，会抛 `CALLER_NOT_AUTHORIZED`。规格 §2.2 只列了目标豁免。调用点已用 `LinkAdmission.governs`（`link_governed` 按名双向豁免）先行过滤，所以不判失败。WP2-B 的 `link_send` 必须先判 `governs`，再问 gate。`admit_dispatch` 的双向豁免正确。
- P-3〔缺陷，严重〕store 丢失更新：并发 drop 时，撤回永久丢失。复现：A–B 已成立；A 铸造 D，产生待写意图。service 线程在 `store.flush()` 写 A 的文件时阻塞，此时 IPC 线程执行 `drop(actor=A, peer=B)`，写入去掉 B 的新意图。flush 完成后，`_commit` 无条件执行 `self._intents.pop(actor)`，把这份新意图删掉。之后又跑了 5 个 tick，A 的已提交文件（rev 7）仍引用 B。B 侧因此持续认为 link 已成立，A 侧却已回收：屏障无法清除，`_retract_single_sided` 也跳过已回收的 peer。同一窗口内的 `admit_send` 铸造也会丢失（探针中丢了 C），但 held 行重评时会重新铸造，可以自愈；撤回不能自愈。根因：IPC 线程（`admit_send` 的 `Mint`/`evict`，以及 `add`、`drop`）直接调用 `store.write`，并在 `_reclaim` 中调用 `inbox.inbound_live`、`ledger_snapshot`、`apply` 与 publisher。这违反规格 §3.5「写文件与 ledger 更新一律交给 service 线程（kick）」，也违反 `LinkStore.write` 自身的「Service thread only」注释。另外，`_commit` 也没有比较版本。
- P-4〔缺陷〕`_seen` 竞争会让 inbox actor 线程抛错。`_refresh_seen` 在 service 线程上不持 `_mem` 就增删 `self._seen`。`admit_dispatch` 经 `_view`、`_ensure_controller`、`_n_active` 遍历同一个 dict。压力探针中，一个线程不断加入新的在线 agent，另一个线程不断调 `admit_dispatch`，`admit_dispatch` 抛出 `RuntimeError('dictionary changed size during iteration')`。这条异常会出现在 inbox actor 线程上，可能还在 SQLite 事务里。G13 与 G14 都没有覆盖这一点。
- P-5〔通过〕两份文件互引才算成立；只有单边引用时 `admit_dispatch` 为 False。
- P-6〔通过〕`settle_hint` 只读，对未知 actor 也不建状态。
- P-7〔观察〕SA11 的断言没有被真正触发。探针重放 SA11：39 个周期里 W 一直等于 k=2，grad 一直是 350。「增量最多 1」和「grad ≤ 0 才增」两条断言从未进入增窗分支；「W 不低于 k」与「grad > 0 不增」成立。§5.6 的 W 塌缩到 k 的路径没有被场景驱动。H16、R5 覆盖了分区；SA13、SA16 覆盖了全审查与无基线；SA15 只检查 `next_eval_ms`，属于结构性断言。

其余语义按代码通读核对，未发现问题：

- `submitted`、`reply_received`、`injected` 都幂等，只改内存。
- 放弃出站时记一次审查 RTT 样本，值为阈值，`censored=True`。
- 回收时先在内存中写排干记录，再调 `apply`；`apply` 失败时，下次 pass 重试 `pending_drain`。
- 排干记录不随屏障清除而失效。
- `LINK_RECLAIMED` 先于其他回复校验检查。
- `link.minted` 与 `link.reclaimed` 带 `side` 与计数。
- store 每个 actor 一个文件，取最高修订号快照，区分意图与已提交、暂缺与冲突。
- controller 与 policy 都调用 WP1 的函数，没有重新实现。

## 3. 接受的偏差

- 包内文件名：用 `_base.py` 放常量与 `_ActorState`，controller 与样本方法留在 `core.py`，没有单独的 `samples.py`。理由是 7 个文件的预算正好用满，且要避免循环导入。接受。
- G12 只覆盖 `sender_known`、`admit_dispatch` 的线程归属；sink 回调那一半归 WP2-B。接受，WP2-B 必须补上。
- G/H/R 的红是骨架回放出的 `NotImplementedError`，强度弱于行为断言红。最终测试断言行为，接受。
- `settle_hint` 只对排干 id 答 `ack`：按规格 §2.2 与 G11，不按 ports 注释的更宽表述。裁定 56 的非 request 行由 WP3-A 在调用前判定。接受。

## 4. 合并检查

- 并入集成分支（`aab662594`）：无冲突。
- 与 `wp2b/send-links`（`86a02f5c7`）合并：无冲突；`links/service.py` 被包取代。

## 5. 集成者合并时要做

1. 合入 WP2-B 后重跑 `tests/daemon/links` 与 WP2-B 的 L/W 组，确认 WP2-B 只经包的公共方法调用引擎。
2. 在 WP2-B 验收中核对：`link_send` 先判 `link_admission.governs`，再调 `admit_send`（P-2）；G12 的 sink 那一半已补上。
3. 删除 `E2E_PERMISSIVE_LINK_GATE_ENV` 与替身，属 WP2-B 范围（§9.4），本分支未动。

## 6. 返工清单

1. P-1：`admit_send` 对 `user:` 目标的 W 拒绝只用于新请求（`reply_to is None`）。校验通过的回复按 §2.2 步骤 1.4 返回 `Allow(None)`。在 test_gate 增加一条「W 满时，回复非 owner 用户的请求仍为 Allow」，并给出先红输出。
2. P-3：IPC 线程不再直接写 store、不再调 inbox 的 ledger 与 publisher。按 §3.5，`admit_send` 的 `Mint`/`evict`、`add`、`drop` 只在内存登记待写与待回收，然后 `kick()`，由 service 线程执行 `store.write`、`_reclaim` 与 `apply`。`drop` 如需同步返回计数，在锁内计算残余，durable 部分交给 service 线程。同时让 `LinkStore._commit` 只在 `_intents[actor] is file` 时删除意图。增加测试：flush 阻塞期间并发 drop 或铸造，放行后几次 tick 内，A 的已提交文件反映最后一次意图。可改写本报告的 P-3 探针。
3. P-4：`_n_active` 不得在无锁的情况下遍历被 service 线程修改的 dict。可在 `_mem` 内更新 `_seen`，或改为整体替换的不可变快照。增加并发测试：一边 `_refresh_seen` 加入新 agent，一边 `admit_dispatch`，不抛错。
4. 补 H14（`test_h14_...`）：一行恰在本次 pass 中到期，只出现在 `expired`，不进 `release`，也不计入 `outbound`。
5. SA11：让场景真正进入增窗分支。断言 W 至少增长一次，每次增长都满足增量 ≤ 1 且 grad ≤ 0；另加一段负载上升，令 W 回落并停在 k 以上。
6. 删除 6 个 `service/*.py` 文件末尾多余的空行，使 `git diff --check 0e552780d HEAD -- ':!docs/evidence'` 无输出。
7. 修正证据 wp2a.md 中「69 engine cases」与「`git diff --check` clean」两处，按返工后的实测重写，并附 P-1、P-3、P-4 的先红输出。
