# WP5-B 第二次验收：REJECTED

- 评审：opus 验收角色，2026-10-15。
- 对象：分支 `wp5b/send-links`，worktree `/private/tmp/hf-wp5b-send-links`，HEAD `50f882e93`。开工时 `git status` 干净。
  第一次验收 `be607e8bf`（REJECTED，4 项）之后的提交：`3e58b5508` 红测 → `86af3231c` 启动修复 →
  `2a518eb98` 文档/证据 → `5a69924b9`、`50f882e93` golden argv。
- 依据：`wp5b-acceptance-1.md` 全文、README 裁定 57、65、66、75、77、`wp5b.md`（更新后）、
  `git diff be607e8bf..HEAD -- src tests docs`。
- 结论一句话：第一次验收的 4 项返工全部落地且有证据；但本分支（第一轮 `f21304dd0` 新增的隐藏选项
  `python -m hyprial.daemon.impl.mcp.entry agent-channel --session-ref-file` 无 help）让 `tests/shell` 的 CLI help 预算门变红，
  集成分支上该测试是绿的。第一次验收只跑了 `tests/daemon`，没覆盖到。一行修复，返工后可直接合。

## 逐项

1. 裁定 77 审批策略 —— PASS。`sessions.py::_interactive_codex_runtime_args`（只在 `_start_interactive_codex` 一处调用）：
   用户未给审批旗标时前置 `--ask-for-approval on-request`；TUI argv 用 `launch_args`，resume 路径
   `server.resume_thread(resume, runtime_args=launch_args)` 同样带上，经 `_thread_execution_params` 译成
   `approvalPolicy=on-request`；显式 `-a untrusted` 或 `--approve-for-me` 原样保留（codex 本身禁止它们与
   `--ask-for-approval` 并用，见下）；`-a never`、`--ask-for-approval never`、`--ask-for-approval=never`、`-a=never`、
   `--dangerously-bypass-approvals-and-sandbox` 在 start 与 resume 两路均 `INVALID_ARGUMENT`，消息含 `on-request`。
   - 先红：scratch worktree `git worktree add --detach … 3e58b5508`（相对 `be607e8bf` 只多测试文件，src 未改），
     `test_codex_interactive_mcp.py` 12 failed / 9 passed：2 条 `assert [] == ['on-request']`，10 条 `DID NOT RAISE CliError`；
     worktree 已移除。
   - golden：`test_start_codex_interactive_golden` 只在 `--no-alt-screen` 后插入 `--ask-for-approval on-request`，
     其余元素与精确 `==` 比较不变；`--model gpt-5.6-luna` 在该测试里来自 `--` 之后的 runtime args，所以位于前置旗标之后，
     与实现 `[--no-alt-screen, *model_args, *launch_args, --remote …]` 一致。是预期 argv，不是放宽。
   - headless 不受影响：scratch 探针 `CliRunner` 跑 `start codex --headless --name x -- -a never`，假 daemon 收到
     `lifecycle.start args=["-a","never"]`，退出 0；`_thread_execution_params(("-a","never"))` 仍得 `approvalPolicy=never`。
   - 真实 codex-cli 0.162.1 实测旗标解析（`codex <args> --remote unix:///nonexistent`）：前置 `--ask-for-approval on-request`
     与 `--approve-for-me`、`--yolo` 并用时 clap 报冲突，与 `-anever` 并用报「cannot be used multiple times」；
     单独 `-anever`、`--yolo` 能通过解析。见返工 2。
2. capabilities 注释与 SKILL —— PASS。`capabilities.py` 的 `("codex", False)` 在 `PROACTIVE_SEND` 上写明 native 以
   on-request 为前提、`-a never` 下 codex 拒绝全部 MCP 调用、启动固定 on-request 并拒 never/bypass（裁定 77），
   `TOOL_INJECTION` 引用同一条件；`src/hyprial/skills/hyprial-ops/SKILL.md` 加了一条 interactive codex 工具面与审批说明；
   Skill-Impact 记在 `docs/evidence/org-structure-constrain-2026-10-14/wp5b/README.md`（PR 正文需转写）。
3. Ruff 归零 —— PASS。`git diff -M --stat be607e8bf..HEAD -- docs/evidence`：5 个探针脚本 `*.py → *.py.txt`，
   重命名相似度均 100%（字节不变），另新增 evidence README；`uv run ruff check .`：`All checks passed!`；
   未改 `pyproject.toml`。
4. wp5b.md —— PASS。第 0 步结论补了：占位值字面为 `pending`；actor 已有注册会话时（register 与写文件之间的窗口，
   或 resume 时旧会话仍在）fence 答 `SESSION_SUPERSEDED`；sessionRef 文件在 `mkdtemp(prefix="hyprial-codex-attach-")`
   私有目录（0700，与 `app.sock` 同目录，`.tmp` 后原子 `replace`，退出 `rmtree`）；另加一条裁定 77 审批口径。
5. 第一次验收 PASS 项回归 —— PASS。焦点集（含 Y1 的 `-c` 挂载、注册时文件不存在/注册后为 thread id、TUI argv 无
   `mcp_servers`）全绿；用第一次验收的真实 `DaemonApplication` fence 探针（scratch 副本，未提交）在 HEAD 复跑：
   未注册且无文件 `STALE_SESSION`；已注册无文件 `SESSION_SUPERSEDED`；外来 ref `ghost` `SESSION_SUPERSEDED`；
   写入 `session-1` 成功返回正文；注册 `session-2` 后旧值 `SESSION_SUPERSEDED`；请求依次携带
   `pending, pending, ghost, session-1, session-1`。与第一次结果逐条一致（探针自身的清理 fixture 误用报错，不影响读数）。
6. 运行 —— FAIL（一条本分支引入的新红）。
   - `PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/harnesses/codex tests/daemon/channels/mcp tests/shell/cli/agents/codex tests/tooling/contracts -q -n 4`：452 passed。
   - `tests/daemon tests/shell -q -n 4`：8 failed、8480 passed、6 skipped、7 xfailed、1 xpassed。其中 7 条为已知红：
     pi controlled_loader t04–06、login/device t11、GUI lifecycle 5 条（env）。
     新红：`tests/shell/cli/presentation/test_cli_help_coverage.py::test_undocumented_parameters_do_not_grow`
     ——「undocumented parameter count is 55, budget is 54」，多出的正是 `python -m hyprial.daemon.impl.mcp.entry agent-channel --session_ref_file`
     （`meta/mcp.py:121`，`hidden=True` 但无 `help=`，来自 `f21304dd0`）。在集成分支 `cf52d2ee8` 的 scratch worktree
     上同一测试 PASS（同次只有 t11 红），确认由本分支引入。它不在已知红名单（已知的是 CLI command inventory，另一条）。
   - 结构限制 `violations: []`；模块边界 `violations: []`。
   - `git diff --check be607e8bf HEAD`：无输出。`git diff --check refactor/org-structure-constrain...HEAD` 两处：
     `wp5b-acceptance-1.md:34`（验收文档行尾空格）与 `wp5b/probe-scripts/probe.py.txt:27`（WORM 探针原字节，不改）。
   - 预算：`sessions.py` 753 行（wp5b.md §2 预算 ≤ 730，超 23），按裁定 75 记录不返工；`capabilities.py` 308。
     `src/hyprial/daemon/impl/links/**` 与任何 `ports.py` 相对 `93a13b4ba` 无改动。
7. 合并就绪 —— PASS。`git merge-tree --write-tree refactor/org-structure-constrain HEAD`（集成分支 `cf52d2ee8`，
   与 origin 同）退出 0，tree `ab8d51a16`，无冲突。两边都改的仍只有 `sessions.py`（集成侧 `_start_interactive_pi`，
   本分支 `_start_interactive_codex` 与新增 helper），合并树中该文件 753 行。
8. Contract-Impact（交 WP8）—— 记录。
   - `support("codex", headless=False)` 的 `PROACTIVE_SEND`、`TOOL_INJECTION`：unsupported → `native("app_server")`，
     前提为 on-request 审批；identity 侧 `proactive_send`/`tool_injection` 对 interactive codex 的取值随之变化。
   - 新增隐藏 CLI 选项 `python -m hyprial.daemon.impl.mcp.entry agent-channel --session-ref-file <path>`；占位 sessionRef 字面 `pending`，
     占位期拒绝码为 `STALE_SESSION` 或 `SESSION_SUPERSEDED`（视 actor 是否已有注册会话）。
   - `hyprial start codex`（interactive，start 与 `--resume`）默认带 `--ask-for-approval on-request`；
     `-a never` / `--ask-for-approval[=]never` / `--dangerously-bypass-approvals-and-sandbox` → `INVALID_ARGUMENT`；
     headless 不变。
   - 真实 TUI 中模型调用工具与审批交互的端到端场景仍 BLOCKED-on-WP8（`blocked.txt`）。

## 接受的偏差

- 第一次验收已接受的三条照旧：方案 P 替代 thread-scoped；ref 文件在启动私有 tmp 目录；`mcp.py` 超预算 5 行。
- `sessions.py` 超 wp5b.md 预算 23 行（裁定 75，记录）。
- 显式 `-a untrusted` 与 `--approve-for-me` 原样保留、不前置 on-request（codex 禁止并用）。附注：codex-cli 0.162.1 的
  `-a` 只接受 `on-request|never`，`untrusted` 会被 TUI 拒绝；测试 `test_interactive_keeps_an_explicit_asking_policy`
  只验证透传，不代表该值可用，不返工。
- 拒绝而非警告（无合法放行旗标），与 SKILL 及 capabilities 注释一致。

## merge-tree 结果

`git merge-tree --write-tree refactor/org-structure-constrain HEAD` → `ab8d51a160fb299429b9c6a68e309dfc5c6ab265`，干净，无冲突文件。

## 集成者合入时要做

- 合入时剥掉 `docs/design/send-links-impl/wp5b-acceptance-1.md:34` 行尾空格（或在 `--check` 中排除验收文档）；
  `probe.py.txt:27` 属 WORM 证据原字节，排除 `docs/evidence` 后检查。
- wp5b.md §2 预算表记实测：`mcp.py` +20、`sessions.py` 753。
- PR 正文写 Skill-Impact（hyprial-ops SKILL 新增 interactive codex 审批条）与 Contract-Impact（第 8 项）。
- 合入后在合并树上复跑本包焦点集、`tests/daemon/harnesses/contracts` 与 `tests/shell/cli/presentation`。

## 返工清单

1. 修 `test_undocumented_parameters_do_not_grow`：给 `src/hyprial/shell/impl/cli/commands/meta/mcp.py:121` 的
   `--session-ref-file` 加 `help=`（保持 `hidden=True`），使未文档化参数计数回到 54；不调高 `_UNDOCUMENTED_BUDGET`。
   验证：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/shell/cli/presentation -q` 全绿。
2. 补齐两种同义写法，让它们也得到 `INVALID_ARGUMENT` 而不是 codex 在 TUI 启动后才报 clap 错误：
   `--yolo`（codex 的 `--dangerously-bypass-approvals-and-sandbox` 别名，单独可通过解析）与短选项贴值 `-anever`
   （clap 接受；当前因前置 on-request 而被 codex 以「cannot be used multiple times」拒）。在
   `test_interactive_rejects_policies_that_disable_harness_tools` 参数表加两例，先红后绿。
