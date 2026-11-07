# WP5-C claude channel 第一次验收

ACCEPTED

- 分支：`wp5c-claude/send-links`，HEAD 34b193ded；基线 ba6ae30dc（WP5-A）。提交：7e1d78578（先红测试 + red.txt）→ e955e9187（实现）→ 34b193ded（证据）。
- 验收人：opus 角色（本地子 agent），2026-10-15。工作树验收开始时 `git status` 干净。
- 依据：wp5c.md §1、§4、§6.3、§7；wp5.md §4、§5、§9.5 残项 2；wp7.md §0.1；README 裁定 56–66、71、72、74、75（71 起的裁定取自集成分支 4c2ff4c59 的 README）。

## 逐项结论

1. 信号 —— PASS
   - `claude.py:278` 会话级 hook 新增 `UserPromptSubmit` → `python -m hyprial.biz.impl.turn_pulse --signal-dir <turns>/start`，timeout 5；`grep -n UserPromptSubmit claude.py` 恰 1 行；golden（test_launch.py）逐字节更新；L5 断言 SessionStart 不含 turn_pulse。
   - start 与 Stop 同一 reportId：L1 断言 `ended[0].reportIds == [started[0].reportId]`。
   - 未闭合 turn 上再来 start → 先报 `ended(status=failed)` 再开新 turn，帧序 started/ended/started（L6）。
   - 迟到 start 被吸收（origin read→pulse），不产生第二个 started（L3）。
   - 转发次序：start/ 与 Stop 脉冲按 `(mtime_ns, start 先于 end, 文件名)` 排序；未确认即中止本轮、脉冲留盘（loops.py `_forward_turn_signals`）。另见探针发现 F1（harness_read 与未转发脉冲之间的次序）。

2. 结算 —— PASS
   - 只有 `harness_read` 的 fetched 读结果进入 `turn_injected`，`kind == notice` 排除；门铃 `apply`/`_remember_messages(observed=False)` 与 `pending.wait` 不计（doorbell 测试 + 探针 P3：第 2 轮读到提醒 notice，settle_hint 只问 r1）。
   - Stop 报告确认之后才 settle_hint（L2 断言 ended 早于 settle_hint）；ack→`message.ack`，keep 不写任何帧，全程无 `message.reply`（L2）。
   - 异常留待重试：探针 P1（settle_hint 传输丢失 2 次）、P2（settle_hint 拒绝 4 次跨轮）：id 留在 `owed_settle`，下轮重试，p1 只 ack 一次，r1 保持 pending，无 reply。注：同一轮末尾会立即再试一次（loops 末尾 `if adapter.owed_settle`），属允许的重试节奏。
   - 显式 ack 后轮末再问幂等（L7：两轮转发不抛、不无限重试，pending 为空）。

3. 持久化 —— PASS
   - 持久内容：`<turns>/state/open-turn`，JSON `{reportId, origin, started}`，临时文件 + `os.replace` 原子写，目录 0o700。`<turns>` = `services._state_dir()/claude-channel-<ref>-<hex>.turns`，在 agent 状态目录，不在仓库。
   - 有界：每次启动一个文件；`cleanup_launch_resources` 在启动收尾 `rmtree(.turns)`，start/ 与 state/ 一并清除。`turn_injected`/`owed_settle` 只在进程内（与 wp5c §4.2 一致）。
   - 子进程重启：新 adapter 构造时 `_load_open_turn` 恢复，同 reportId 重试，只 1 个 `turn.completed`（L4）。
   - 与裁定 64 不冲突：裁定 64 只免除 pi 的持久化，并写明「codex、claude 与 headless 按 B7 保留并重试」；wp5.md B7 描述 claude 现有 Stop 信号保留到上报成功，本实现把同一语义扩到 start，一致。

4. WP7 交互 —— PASS
   - 测试夹具 `_TurnApp` 取 `shell.impl.daemon_wiring.daemon_dependencies()` 的 `reply_reminder_consumer_factory`，即真 `biz.ReplyReminderConsumer`，经真 `wire_reply_reminder_hooks` 接线，不预置 last_end。L1：新会话首 turn start→fetched 读→Stop 后得 1 条提醒且含 r1。WP7 消费者未改（`git diff --name-only ba6ae30dc..HEAD -- src/hyprial/biz` 为空）。无需另行探针。

5. 先红 —— PASS
   - 7e1d78578 只含测试与 red.txt，无 src。
   - 本人在 scratch worktree（ba6ae30dc + 7e1d78578 的 tests，已删除）复现：不垫片 → 7 条 `TypeError: unexpected keyword argument 'turn_signal_dir'`；只给 `__init__` 加 `turn_signal_dir` 形参的垫片 → 8 条真实断言失败：L1 `wait_for` 为 False（无提醒）、L2 `[] == ['e1','p1','r1']`、L3 `('session.turn.started', None) in [('message.pending.list', None)]`、L4/L6/L7/doorbell `0 == 1`（无 started）、L5 `KeyError: 'UserPromptSubmit'`。
   - 既有测试改动未削弱断言：test_mcp_channel.py 只给假 daemon 脚本插入 started 应答，并把 `calls[0]` 改为先断言 `calls[0] == session.turn.started` 再查 `calls[1]`（更强）；test_mcp_channel_restart.py 只给 FakeDaemon 答 turn 帧、给脚本化 adapter 加 `open_turn`/`owed_settle` 属性。

6. 运行与门 —— PASS
   - 焦点：`tests/daemon/channels tests/daemon/harnesses/carrier tests/biz/hooks tests/shell/cli/agents/claude` → 285 passed, 2 xfailed。
   - 宽：`tests/daemon tests/shell -n4` → 7 failed, 8279 passed；7 条全在已知红名单（pi controlled_loader t04-06、GUI env ×5、login/device t11）。
   - `check_structural_limits` violations []；`check_module_boundaries` violations []、rc 0；`ruff check .` 通过；`git diff --check ba6ae30dc HEAD -- ':!docs/evidence'` 干净。
   - 行数：claude.py 499/500、loops.py 402/470、adapter.py 587/540（超，见偏差 D1）；`mcp/channel/` 直接文件数 6→6。
   - wp5c §7 第 3 条 claude 相关禁区 diff（contract.py、inbox、runtime、hooks、application、capabilities、biz、kernel、links）为空；`ports.py` 未动。

7. 合并就绪 —— PASS（见下节）

8. Contract-Impact（交 WP8）—— 记录，非阻断。见「集成者合并时要做」。

9. UNKNOWN —— BLOCKED-on-WP8
   - Claude Code 对 channel 推送启动的 turn 是否触发 `UserPromptSubmit` 未知。设计两种情形均成立：触发 → L1 路径；不触发 → harness_read 先同步补报 started 再 fetched 读，Stop 闭合（L3）；迟到触发 → 吸收（L3）。模型不调 harness_read 且无 start 脉冲的推送 turn 仍走 WP7 规则 2 的旧 Stop 上报。真实 TUI 联测 BLOCKED-on-WP8。

## 探针发现（scratch 测试，未提交）

- F1（非阻断，设计缺口，建议 WP8 前补）：Stop 脉冲只在 poll 循环的 list 轮之后转发，上一 turn 的 Stop 可能在盘上停留到下一个 list（轮询间隔或 pending.wait 持有时长）。此间下一 turn 的 `harness_read` 看到 `open_turn` 仍是上一 turn，于是不补报新 turn，而把本轮读到的 id 记进上一 turn。探针：t1 Stop 脉冲未转发 → 新请求 r2 → harness_read → 转发：t1 结束时即对 r2 发提醒「1 条请求未回复：r2」，r2 的 settle_hint 记在 t1；有 t2 的 UserPromptSubmit 脉冲时同样（t2 之后 started，turn_injected 为空）。后果：提醒可能早于模型处理完 r2（误提醒），reply/event 在上一 turn 结束时被 ack；无代答、无数据丢失、request 仍 keep。wp5c §4.2 只规定 open_turn 为 None 时补报，未要求 harness_read 先消化盘上脉冲，实现符合规格。建议修法：harness_read 在 `_turn_lock` 内先处理 `<turns>` 与 `<turns>/start` 中已存在的脉冲（或至少处理比 open_turn 更新的 Stop），再决定是否补报。
- F2（非阻断）：`apply_turn_end` 先 `_clear_open_turn` 再由 loops 删 Stop 脉冲；两步之间崩溃 → 重启后 open_turn 为空、Stop 脉冲仍在，按旧路径以脉冲名再报一次 ended，产生第 2 个 `turn.completed`（探针实测 2）。窗口很窄。若先删脉冲再清 open-turn，崩溃后残留的是已结束的 reportId，daemon 按 reportId 去重即无副作用。
- F3（非阻断，与 wp5.md §5 的偏差）：kept 的 request 下一 turn 再被 harness_read 返回时会再进 `turn_injected`，轮末再问一次 settle_hint（探针 P3 第 2 轮问 r1）。§5 写「载体记住 completed/kept，同一会话内对同一 id 只结算一次」，同时又以「本 turn 注入过」为范围；claude 的 harness_read 每次返回全部 pending，重问是只读且 keep 不写，若 gate 期间改答 ack 则结算正确。接受，记录。
- 其它探针均符合预期：notice 不入应结算集合；settle_hint 失败不导致 ack、不重复 ack。

## 接受的偏差

- D1 adapter.py 587 行，超 wp5c §1 预算 540 行 47 行，低于 1000 行硬门。按裁定 75（预算不是门）接受。判断：新增约 156 行中，持久化（save/load/clear，约 35 行）与 turn 状态机（apply_turn_start/end、_ensure_started、_note_injected）均为 §4.2 规定内容；`settle_owed`（约 37 行）与 `contract.settle_injected` 逻辑重复，但 `CarrierPort`/`settle_injected` 是同步接口而 channel adapter 只有异步 daemon proxy，WP5-A 文件在本包禁区内不能加异步变体，重复有正当理由，不是隐藏的大块复制。语义差异：未知 hint 在 adapter 中被保留重试，`settle_injected` 视为 kept；`settle_hint_for` 只会答 ack/keep，实际不可达。集成者按裁定 75 把实测 587 写进 wp5c.md §1。
- D2 `harness_read` 在 started 未确认时向工具调用抛错，不做 fetched 读（规格要求的行为，记录以便 WP8 观察真实 TUI 下的报错呈现）。

## merge-tree

- 对 `refactor/org-structure-constrain`（4c2ff4c59）：`git merge-tree --write-tree` rc 0，无冲突。集成分支自基线起只在本包相关目录改了 `tests/daemon/channels/mcp/contracts/test_mcp_daemon_alignment.py`，本包未动该文件。
- 对 `wp5c-pi/send-links`（d8caa3abb）：rc 0，无冲突，无共用文件。
- 对 `wp5c-codex/send-links`（2370a2a04，进行中）：rc 0，无冲突，无共用文件。
- 四方合并实测：scratch worktree 在 4c2ff4c59 上 octopus 合并 34b193ded、d8caa3abb、2370a2a04 成功；合并树 structural violations []；焦点套件 285 passed, 2 xfailed。scratch worktree 已删除。

## 集成者合并时要做

1. 把 adapter.py 实测 587 行写入 wp5c.md §1 预算表（裁定 75），PR 正文说明理由（同 D1）。
2. Contract-Impact（交 WP8 汇总）：claude 启动 settings 新增 `UserPromptSubmit` hook（golden 已更新）；channel 新发 `session.turn.started`（含 harness_read 补报）与带 `status: failed` 的 `session.turn.ended`；channel 轮末调用 `message.settle_hint` 并据此 `message.ack`。均为 WP5-A 已定义的 IPC，无新字段；`contract/` 是否需记录 channel 侧的使用由 WP8 决定。
3. 把 F1 列入 WP8 前的跟进（或单列小返工）：harness_read 先消化盘上脉冲；同时建议 F2 的删除次序调整。
4. WP8 真实 Claude Code TUI 联测：确认 channel 推送 turn 是否触发 `UserPromptSubmit`、harness_read 补报失败时的工具报错呈现、F1 场景是否出现误提醒。
5. 合并后在集成分支重跑焦点套件与 `tests/daemon tests/shell -n4`，已知红名单不变。
