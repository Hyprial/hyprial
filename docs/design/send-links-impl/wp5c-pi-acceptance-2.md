# WP5-C pi attach 第二次验收（opus，2026-10-15）：ACCEPTED

分支 wp5c-pi/send-links，HEAD f71025a27；第一次验收 a4c6c4213（REJECTED，5 项返工）之后提交 55c604a4d（红测试 + 恢复测试 + 两条守护）→ ff9b38345（修复）→ f71025a27（证据追加）。开始时 git status 干净。全部命令经 uv run 使用 worktree 自带 .venv。

结论：返工 5 项全部完成，丢失唤醒已修且有确定性红绿测试；第一次验收的 PASS 项无回归。唯一新偏差是为压到 800 行把 sendHeadReport 的三元合成一行，使 wp5c §7.4 的按行计数从 2 变成 1（字面量仍恰 2 个），按裁定 75 记为偏差，由集成者在合并时还原（见下）。

## 逐项

1. 丢失唤醒 PASS。flushReports 提前返回分支改为 `return armReportTimer()`；runPollLane 每轮 `if (reportQueue.length > 0) void flushReports()`（wp5c §2.3「poll 道每轮尝试队首」）；armReportTimer 只在无定时器时按 reportRetryAt 剩余时间排一个。新测试 test_report_retry_survives_a_timer_that_fires_before_the_deadline 用驱动钩子 skew_clock 让 Date.now() 落后 30ms 共 150ms，使定时器相对截止时间"提前"触发。复现：临时 worktree（scratchpad/red2，--detach 55c604a4d，src 与 a4c6c4213 无差异，即修复前代码；已确认加载的是该 worktree 的 TS）单跑 5 次 5 次红，失败点均为 wait_until 超时（ended 永不被接受）；HEAD 单跑 20/20 绿。整个 test_pi_carrier_contract.py（18 条）本人连跑 30 次 30/30 通过；P9 单跑 20 次 20/20（第一次验收为 18/20）。变异：只去掉早返回的 armReportTimer 或只去掉 poll 道 flush，新测试与 P9 各 3 次仍绿；两者都去掉则红。即测试守的是二者之并，单一路径各自无独立守护；规格本就允许「二者择一或都做」，接受。临时 worktree 已移除。
2. 恢复的 ambiguous 测试 PASS。与 ba6ae30dc 中被删版本对比：同一 origin（candidateOwners 空、candidateUsers 两个 guest）、同一整串相等断言，头部文字逐字不变，仅正文由 "status?" 改为 expected_block（summary-v1 块），并补 bodyBytes=7。断言强度不变（仍是整串 ==）。
3. 两条守护 PASS。(a) test_no_settle_hint_or_ended_between_agent_end_and_agent_settled：agent_end 后 8 个 poll 周期无 settle_hint、无 ended，settled 后恰 1 次 settle_hint。变异「poll 道重试条件去掉 outstanding.settled」后该测试红。(b) test_transient_settlement_failure_is_retried_without_reinjection（settle_hint、ack 两个参数化）：前两次 DAEMON_BUSY，断言 ≥3 帧、最终 ack、注入 1 次、无 reply。变异「agent_settled 失败分支加一次 inject」→ 2/2 红；变异「删除 poll 道结算重试」→ 2/2 红。
4. 证据 PASS。git diff a4c6c4213..HEAD -- docs/evidence 只有新增行（gates +12、green +4、red +4、pi-frames.json +188，删除 0）。pi-frames.json：P3 为 register → list → turn.started → fetched 读 → turn.ended(completed) → settle_hint m-1 → ack m-1 → settle_hint m-2（无 ack，即 keep）→ 后续只有 poll；P4 同序且只一条；P9 同一 reportId 的 started 发 3 次（前两次失败）后 ended(completed) 带同一 reportId。均与假 daemon 的程序化失败及规格顺序（started 被接受后才 fetched 读、settle 在 ended 入队后）一致，无 message.reply。小缺：green.txt 未单列「P9 单跑 30 次」（第一次验收建议），以整文件 30 次覆盖；本人已补 P9 单跑 20 次。
5. 注释与行数 PASS（含一条偏差）。REPORT_RETRY_CAP_MS 注释改为「ceiling of the turn-report retry backoff」，正确。pi_harness_attach.ts 恰 800 行。压缩核查：sendHeadReport 先取 `const started`，语义不变；reportFailures/reportRetryAt 合为一行赋值，语义不变；tmux 注释由 7 行缩为 4 行，保留「daemon 记录 session 名供 hyprial ps」「载体进程内、生命周期等于 pane」「shutdown best-effort 注销、TTL 回收」，丢失「无需 claude 式 owner-fence 重接」的显式说明与 kill-session → SIGHUP 链；属理由性而非协议性，判为可接受的轻微损失。偏差：三元合为一行后 `grep -c '"session.turn.started"\|"session.turn.ended"'` 由 2 变 1（a4c6c4213 为 2），wp5c §7.4 要求 2；`grep -o … | wc -l` 仍为 2，语义上依旧恰两个 turn 方法字面量，无额外调用点。worker 的 rework gates.txt 未重记 counts，此回退未自报。按裁定 75（行数预算非门，超出按实测记录不返工）：拆成两行（`started ? "session.turn.started"` 与 `: "session.turn.ended",`）后本人实测 801 行、计数 2，记录该数，不返工。
6. 纪律 记录（不阻断）。worker 自报两次编辑脚本与一次证据单行用了裸 python3，违反「只用 uv」。核查：git status --untracked-files=all 为空；被忽略的只有常规 .pytest_cache/.ruff_cache/__pycache__（任何 uv 下 pytest 运行都会产生，本人运行也会），无多余文件、无产品 import 痕迹。记为失误，不影响结论。
7. 第一次验收 PASS 项回归 PASS。tests/daemon/harnesses/pi + carrier 全跑覆盖：硬切（G3p/G4p/G5 及 §7.3 第一条 grep 为空、"sends your final reply back" grep 为空）、注入共享 golden（P2 与 carrier K2）、裁定 64（P10 重启边界）、R3 rewake（P7、P10）、结算（P3、P4、P6、P8）均绿；§7.4 settle_hint 计数 1。
8. 运行 PASS。HYPRIAL_REQUIRE_PI_SDK=1 下 tests/daemon/harnesses/pi tests/daemon/harnesses/carrier：222 passed、2 xfailed、1 failed（已知 controlled_loader t04-06）。tests/daemon -n 4：6660 passed、1 failed（已知 controlled_loader t04-06）、5 skipped、7 xfailed、1 xpassed（ipc heartbeat ratio 等其它已知红本次未出现）。structural limits violations 为空；module boundaries violations 为空；ruff All checks passed；git diff --check ba6ae30dc HEAD -- ':!docs/evidence' 干净；links/**、ports.py 及 §7.3 禁区 diff 为空。
9. 合并就绪 PASS。集成分支 refactor/org-structure-constrain = 3971319d6（含 e8218ceae，即 WP5-C claude、codex 已合入；ba6ae30dc 为其祖先）。git merge-tree --write-tree refactor/org-structure-constrain HEAD 退出码 0，树 f9a2a27ec，无冲突文件、无冲突 hunk。合并树上 pi_harness_attach.ts 800 行、含 armReportTimer、无 "message.reply"；"sends your final reply back" grep 为空。合并引入的路径仅 pi 相关 src/tests、sessions.py、wp5c/pi 证据与 wp5c-pi-acceptance-1.md。
10. Contract-Impact（交 WP8）沿用第一次验收四点，无新增：summary-v1 注入与 pi 批量头文字；session.turn.started/ended 按 reportId 重试、进程重启丢弃（裁定 64）；轮末 settle_hint、只按 ack 执行 message.ack、不再 message.reply（影响 WG-001 autoReply 解释）；sessions.py pi 追加提示文字。本轮返工未改任何已发布字段语义。
11. 真实 pi TUI BLOCKED-on-WP8，同第一次验收，blocked.txt 不变。

## 探针与运行计数（临时，未提交）

- 修复前代码上新测试：5/5 红。HEAD 上新测试：20/20 绿。
- test_pi_carrier_contract.py 全文件：30/30。P9 单跑：20/20。
- 变异 5 组：仅去早返回重排 → 绿（3 次）；仅去 poll 道 flush → 绿（3 次）；两者都去 → 红；守护 (a) 变异 → 红；守护 (b) 两种变异 → 各 2/2 红。
- pi + carrier 全跑 1 次；tests/daemon -n 4 全跑 1 次。

## 接受的偏差

- 第一次验收接受的四条偏差继续有效。
- 丢失唤醒的两处修复由一条测试守其并集，单一路径无独立测试。
- turn 方法按行计数为 1（字面量 2 个），见第 5 项；按裁定 75 处理。
- tmux 注释删去 owner-fence 不需重接的理由句。
- 裸 python3 使用，见第 6 项。

## 集成者合并时要做

1. 在合并提交中把 sendHeadReport 的方法三元拆回两行（"session.turn.started" 与 "session.turn.ended" 各占一行），使 §7.4 计数恢复为 2；文件实测 801 行，按裁定 75 写进 wp5.md §1.4 / wp5c §1 的实测数并在 PR 正文说明。可顺带恢复一句「无需 claude 式 owner-fence 重接」注释（再 +1 行，同样记实测）。
2. PR 正文写 Contract-Impact 四点与 Skill-Impact（hyprial-ops：pi interactive 不再轮末自动回复）。
3. 与 codex/claude 两条无合并次序依赖，merge-tree 无冲突。
