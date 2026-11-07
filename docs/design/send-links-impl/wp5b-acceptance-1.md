# WP5-B 第一次验收：REJECTED

- 评审：opus 验收角色，2026-10-15。
- 对象：分支 `wp5b/send-links`，worktree `/private/tmp/hf-wp5b-send-links`，HEAD `c7a7b6887`，base `93a13b4ba`。
  提交：`13d5339ea` 探针 + wp5b.md → `d348d0091` 红测 → `f21304dd0` 实现 → `c7a7b6887` 证据。开工时 `git status` 干净。
- 依据：wp5b.md 全文（含第 0 步结论）、README 裁定 57、65、66、75、77、wp5.md §9.4/§9.6 与评审 A4。
- 结论一句话：探针、挂载、懒读 sessionRef、fence、测试、预算都成立；但裁定 77（interactive codex 以 `on-request` 启动、
  native 声明写明该前提）在分支上没有落地，且分支新提交的探针脚本让 `uv run ruff check .` 出 54 个错。两处都要返工。
  裁定 77 晚于交付发布（集成分支 `526ee8f73`），不算 worker 违规，但合入前必须满足。

## 逐项

1. 探针结论有证据 —— PASS。`probe.txt` 写明 codex-cli 0.162.1、隔离 CODEX_HOME、本地 Responses 桩、命令与原始事件行：
   方案 T 首轮前 `no rollout found`，首轮后第二连接 resume 带 config 返回成功但无 `mcpServer/startupStatus/updated`、
   `mcpServerStatus/list` 为空、模型工具表无 harness-bridge；方案 P `-c mcp_servers.harness-bridge.*` 下 TUI 线程
   `startupStatus` 带 threadId、status ready。本机复跑 `probe-scripts/smoke.py`（真实 codex 0.162.1 + 生成的 `-c` 参数 +
   真实 `python -m hyprial.daemon.impl.mcp.entry agent-channel`）：`runtimeStatus connected`，harness_* 工具全在，与 `smoke-mount.txt` 一致。
   裁定 65 的条件（fence 按 sessionRef 生效）用真实 `DaemonApplication` 探针核（scratch，未提交）：
   `SessionRefFileProxy` + `create_mcp_server` + `app.handle(..., _trusted_message_origin="ipc")`，`harness_read_body`：
   - 未注册、文件不存在：签 `pending`，答 `STALE_SESSION`；
   - 已注册、文件未写：签 `pending`，答 `SESSION_SUPERSEDED`（见探针发现 2）；
   - 文件写入外来 ref `ghost`：`SESSION_SUPERSEDED`，拒；
   - 文件写入 `session-1`：成功返回正文，请求带 `sessionRef=session-1`；
   - 新会话 `session-2` 注册后旧文件值：`SESSION_SUPERSEDED`，拒。
   文件位置：`_start_interactive_codex` 的 `tempfile.mkdtemp(prefix="hyprial-codex-attach-", dir="/tmp")` 根下的
   `session-ref`（与 `app.sock` 同目录，目录 0700，文件按默认 umask；先写 `.tmp` 再 `replace` 原子发布；finally 中 `rmtree`）。
   不在 agent state dir 内，但属启动私有目录，满足裁定 77「私有文件」，按偏差接受。
2. 裁定 77 审批策略 —— FAIL。`_start_interactive_codex` 的 TUI argv 为 `codex [provider_args] [resume] --no-alt-screen
   [--model] *runtime_args --remote unix://…`，既不写 `-a on-request`，也不拒 `-a never` /
   `--dangerously-bypass-approvals-and-sandbox`（`_reject_codex_session_args` 只拒传输类旗标）；resume 路径
   `server.resume_thread(resume, runtime_args=…)` 同样只按用户参数定 `approvalPolicy`。审批策略因此由用户参数与
   codex 配置决定，`-a never` 时工具挂上却全被拒（probe.txt 实测）。`capabilities.py` 167 行注释未写该前提；
   `src/hyprial/skills/hyprial-ops/SKILL.md` 无 interactive codex 的说明。
3. 测试 —— PASS（附注）。`tests/daemon/harnesses/codex/test_codex_interactive_mcp.py` 8 个测试：Y1–Y7 +
   `test_interactive_config_args_carry_no_session_ref`。都经真实 `create_mcp_server` 工具面（InMemoryTransport），
   `harness_reply`（replyTo、summary、actor、sessionRef=thread、行闭合）、`harness_read_body`（正文、fetched）、
   `harness_ack`（不欠回复闭合，欠回复拒）、`harness_send` 缺 summary 被参数校验拒且未达 daemon、fence（占位
   `STALE_SESSION`、发布后接受、被取代 `SESSION_SUPERSEDED`）各有覆盖；Y1 走真实 CLI `start codex` 流程，断言
   `-c` 挂载、actor 为规范 URI、注册时文件不存在、注册后文件为 thread id、TUI argv 无 mcp_servers。
   先红复现：scratch worktree `git worktree add --detach … d348d0091`（其 src 与 `13d5339ea` 相同），逐个运行：
   Y1 `AssertionError {"ok":false,"error":"'ref_file'"}`（未挂载），Y2–Y6 `ImportError SessionRefFileProxy`，
   额外一条 `ImportError interactive_channel_config_args`，Y7 `AssertionError UNSUPPORTED is NATIVE`；
   contracts golden 两条在红提交同样失败。worktree 已移除。
   golden 改动：`UNSUPPORTED/None` 改为 `NATIVE/"app_server"`，断言强度不变；coordinators 集合为 `==` 精确比较，
   仅加 `("codex", False)`，未放宽。fake daemon 的 fence 是简化替身，真实 fence 由第 1 项探针补证。
   真实 codex TUI 中模型调用工具（含审批交互）BLOCKED-on-WP8，已记 `blocked.txt`。
4. 范围与预算 —— PASS（记录）。`mcp.py` +20（预算 +15，超 5，按裁定 75 记入 wp5b.md 预算表即可，不返工）；
   `app_server.py` 820；`sessions.py` 716（≤ 730）；`capabilities.py` 304；`native_env.py`、`carrier.py`、
   `settlement.py`、`daemon/impl/mcp/**`、`carrier/`、`kernel`、`links/**`（含 `ports.py`）、`inbox`、`biz` 均无改动；
   `harnesses/codex/` 仍 7 个文件 + `projection/`；测试目录 6 → 7。
5. 运行 —— 测试 PASS；ruff FAIL。
   - `PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/harnesses/codex tests/daemon/channels/mcp tests/shell/cli/agents/codex tests/daemon/harnesses/contracts -q -n 4`：458 passed。
   - `tests/daemon -q -n 4`：2 failed、6867 passed、5 skipped、7 xfailed、1 xpassed；两条失败均为已知红（见下）。
   - 结构限制 `violations: []`；模块边界 `violations: []`；`git diff --check 93a13b4ba HEAD -- ':!docs/evidence'` 无输出。
   - `uv run ruff check .`：54 errors，全部在本分支新增的 `docs/evidence/org-structure-constrain-2026-10-14/wp5b/probe-scripts/`
     （probe2.py 17、probe.py 15、smoke.py 14、mock.py 8；多为 E701/E702 一行多语句、未用导入）。
     `pyproject.toml` 只排除 `docs/notes/repro`，集成分支本身 ruff 干净。worker 报的「40 个、不在我的文件」不成立：
     来源正是本分支提交的探针脚本。
6. 合并就绪 —— PASS。`git merge-tree --write-tree refactor/org-structure-constrain HEAD`（集成分支 `cf52d2ee8`，含
   `e95f13a96`、`526ee8f73`）干净，tree `bb2812d8b`。两边都改的只有 `sessions.py`：集成侧改 `_start_interactive_pi`
   的 append_system_prompt（约 159 行），本分支改 `_start_interactive_codex`，不重叠。
7. Contract-Impact（交 WP8）—— 记录。
   - `support("codex", headless=False)` 的 `PROACTIVE_SEND`、`TOOL_INJECTION` 由 unsupported 改为 `native("app_server")`；
     `contract/e2e/run-resident-providers.py` 只读 headless 声明，不受影响。identity 侧运行时 capabilities 字段
     `proactive_send`/`tool_injection` 对 interactive codex 的取值随之变化，WP8 契约需写。
   - 新增隐藏 CLI 选项 `python -m hyprial.daemon.impl.mcp.entry agent-channel --session-ref-file <path>`；占位 sessionRef 为字面值 `pending`。
   - 审批策略：interactive codex 以 `on-request` 启动（裁定 77，返工后落地），`-a never` 只用于 headless。
   - 真实 TUI 模型调用 + 审批交互的端到端场景归 WP8（BLOCKED-on-WP8）。

## 全量结果

`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon -q -n 4`：2 failed、6867 passed。失败为
`pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary`
与 `ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load`，都在已知红名单内，与本包无关；
kernel POSIX listener fd、wait_without_polling 本轮未红。

## 探针发现

1. 真实 codex 0.162.1 下方案 P 可复现；`-c` 参数只含解释器路径、actor、ref 文件路径与 `HYPRIAL_HOME`，app-server argv 中无凭据。
2. 占位窗口的拒绝码不总是 `STALE_SESSION`：actor 已有注册会话（`session.register` 与写文件之间的极短窗口，或
   `resume` 时旧会话仍在）时，`pending` 被 fence 判为 `SESSION_SUPERSEDED`。worker stdio 服务器无 supersede 闩锁，
   逐调用判定，文件写入后即恢复，因此不影响功能；只是 wp5b.md 与裁定 77「fence 答 STALE_SESSION」的表述不完整。
3. interactive 线程拿到的是 headless worker 的完整工具面（含 `workflow_complete/fail/expansion_plan` 与 orgfs_*），
   因为复用 `serve_worker_stdio`（`workflow_tools=True`）。授权仍在 daemon 侧，记录不返工。

## 接受的偏差

- 方案 P（app-server 级挂载）替代裁定 57 的 thread-scoped，按裁定 65、77 接受。
- ref 文件位于启动私有 tmp 目录（0700）而非 agent state dir。
- `mcp.py` 超预算 5 行（裁定 75）。
- Y5 断言为参数校验拒绝（summary 为工具必填参数），未断言 `SUMMARY_REQUIRED` 码；wp5b.md §3 允许按实施 base 写。

## 集成者合入时要做

- 合入后在合并树上复跑本包焦点测试与 `tests/daemon/harnesses/contracts`。
- wp5b.md §2 预算表记 `mcp.py` 实测 +20。
- WP8 契约与 E2E 补第 7 项三条及真实 TUI 场景。

## 返工清单

1. 落实裁定 77：`_start_interactive_codex` 在用户未给审批旗标时为 TUI argv 加 `-a on-request`；resume 路径
   `resume_thread` 的 `approvalPolicy` 同样为 `on-request`；用户显式给 `-a never` 或
   `--dangerously-bypass-approvals-and-sandbox` 时以 `INVALID_ARGUMENT` 拒绝（或经集成者确认改为 stderr 警告并在
   输出标明工具不可用）。先红测试：Y1 同款假进程断言 TUI argv 含 `-a on-request`，并断言 `-a never` 的处理。
2. `capabilities.py` 的 `("codex", False)` 注释写明 native 以 `on-request` 启动为前提；
   `src/hyprial/skills/hyprial-ops/SKILL.md` 补一条 interactive codex 的工具面与审批说明（PR 写 Skill-Impact）。
3. 让 `uv run ruff check .` 归零：整理 `wp5b/probe-scripts/*.py`（拆一行多语句、删未用导入），或改为非 `.py` 扩展名
   存档；不改 `pyproject.toml` 的排除表。
4. wp5b.md 第 0 步结论补一句：占位期在 actor 已有注册会话时 fence 答 `SESSION_SUPERSEDED`（探针发现 2）。
