# WP5-B：interactive codex 的 harness-bridge MCP 面（裁定 57）

- 版本：v2，2026-10-14（从 [wp5.md](wp5.md) v2 拆出；并入评审 A4）。总览与禁区见 wp5.md §1。
- 分支：`wp5b/send-links`。前置：WP5-C 合入（codex 结算与 turn 上报已按契约）。集成分支在本包合入前不合 dev。
- 目标：interactive codex 会话里的模型能用 `harness_reply`、`harness_ack`、`harness_read_body`、`harness_send`（含 summary），身份与 headless 同样受 `(actor, sessionRef)` fence；`capabilities.py` 中 `("codex", False)` 的 `PROACTIVE_SEND`、`TOOL_INJECTION` 改为 native，且每项 native 都有线程工具调用证明。
- ⛔ 不改：结算、注入、turn 上报（WP5-C 产物）；MCP 工具定义与参数（WP6 的 `mcp/server.py` 工具面，原样复用）；`kernel`、`links`、`inbox`、`biz`。

## 1. 已知事实与待证点

- headless 用 thread/start、thread/resume 的 `config` 挂 thread-scoped MCP（`codex/native_env.py:658` `_worker_channel_config`，服务名 `HARNESS_BRIDGE_MCP_SERVER_NAME = "harness-bridge"`），stdio 服务器为 `python -m hyprial.daemon.impl.mcp.entry agent-channel --actor … --session-ref …`（`serve_worker_stdio`，固定 actor 与 sessionRef）。
- interactive codex 由 `shell/.../agent/runtime/sessions.py::_start_interactive_codex` 启动：先起 `CodexInteractiveAppServer`（插件 MCP 经 `config_args` 挂在 app-server 进程上，「Plugin MCP servers run in the app server, not the remote TUI」），TUI 以 `--remote` 连入并自行发 thread/start；sessionRef 在 app-server 启动之后才发现并注册（`session.register`，runtime `codex_interactive`）。
- 因此 thread-scoped 挂载与固定 sessionRef 都有时序问题：thread/start 由 TUI 发，不经 hyprial；app-server 启动时 sessionRef 尚未确定。
- `native_env.py` 注释记录：`-a never` 下 codex 拒绝 MCP 工具调用（需要审批），`-a on-request` 下 native reply 成功。interactive 的审批策略由用户 TUI 决定。

**第 0 步结论（2026-10-15 已执行，原始证据 `docs/evidence/org-structure-constrain-2026-10-14/wp5b/probe.txt`，codex-cli 0.162.1，隔离 CODEX_HOME + 本地 Responses 桩）**：
- 方案 T 不可行：thread 首轮前 `thread/resume` 报 `no rollout found`；首轮后第二连接 resume 带 `config` 成功返回，但无 `mcpServer/startupStatus/updated`、`mcpServerStatus/list` 为空、模型工具表不含 harness-bridge。只有发 `thread/start` 的连接（interactive 下是 TUI）能挂 thread-scoped MCP。
- 方案 P 可行：`codex app-server -c mcp_servers.harness-bridge.*` 对该 app-server 创建的每个线程（含 TUI 线程）挂载，`startupStatus` 带 threadId。按裁定 65 采用，不再另行请示。工具在模型侧为 `mcp__harness_bridge::<tool>`。
- 审批：`-a never` 下工具已挂载但调用被拒（`MCP tool call requires approval, but approval policy is never`），`default_tools_approval_mode`/per-tool approve 均无效；`-a on-request` 下审批 elicitation 送到 TUI，由用户批准。故 interactive 的 native 声明受用户审批策略约束（与 headless 2026-09-05 测量一致），`-a never` 的 interactive 会话不能用这些工具——记入 `blocked.txt`。
- 取舍：app-server 级挂载对该 app-server 上所有线程生效（interactive 只有 TUI 一个线程）；归属由 `(actor, sessionRef)` fence 保证，sessionRef 经 `--session-ref-file` 延迟读取，注册前为占位值（字面 `pending`），daemon 答 `STALE_SESSION`；但 actor 已有注册会话时（`session.register` 与写文件之间的极短窗口，或 `resume` 时旧会话仍在），占位值被 fence 判为 `SESSION_SUPERSEDED` 而非 `STALE_SESSION`（验收探针发现 2；worker stdio 逐调用判定，文件写入后即恢复）。sessionRef 文件位于启动私有临时目录（`tempfile.mkdtemp(prefix="hyprial-codex-attach-")`，目录 0700，与 `app.sock` 同目录，先写 `.tmp` 再原子 `replace`，退出时 `rmtree`），不在 agent state dir 内。
- 审批（裁定 77）：interactive codex 启动与 resume 都带 `--ask-for-approval on-request`；用户给 `-a never` / `--dangerously-bypass-approvals-and-sandbox` 时以 `INVALID_ARGUMENT` 拒绝（不存在合法放行旗标）；`-a never` 仅用于 headless。

**第 0 步（实施前，先报再做）**：在隔离 home 用真实 codex（≥ 0.152.0）实测两种挂法，结果写 `docs/evidence/org-structure-constrain-2026-10-14/wp5b/probe.txt`：
- 方案 T（thread-scoped，裁定 57 原文）：carrier 发现线程后对同一 thread 发 `thread/resume` 带 `_worker_channel_config` 的 config，观察 `mcpServer/startupStatus/updated = ready` 是否绑定到 TUI 正在用的线程。
- 方案 P（app-server 进程级）：`config_args` 追加 `mcp_servers.harness-bridge`，服务器以 `--session-ref-file <path>` 或环境变量延迟读取注册后写入的 sessionRef；未写入前所有工具调用返回 `STALE_SESSION`。
- 两者都不可行时 BLOCKED，报集成者。只有方案 P 可行时，因与裁定 57 的「thread-scoped」措辞不同，先报集成者确认再实施（wp5.md §9.5 第 9 条）。

## 2. 文件（按第 0 步结论二选一；预算为本分支实测）

| 文件 | 现 | 动作 | 预算 |
| --- | --- | --- | --- |
| `daemon/impl/harnesses/codex/carrier.py` | 727 | 不改（方案 T 已否） | 0 |
| `daemon/impl/harnesses/codex/native_env.py` | 961 | 复用 `_worker_channel_config`；不复制 | ≤ 975 |
| `shell/impl/cli/commands/agent/runtime/sessions.py` | 702 | 方案 P：`config_args` 追加、写 sessionRef 文件 | ≤ 730（实测 756，超 26，裁定 75 记录不返工） |
| `shell/impl/cli/commands/meta/mcp.py` | 实施时测 | 方案 P：`agent-channel` 增 `--session-ref-file` | 净增 ≤ +15 |
| `daemon/impl/harnesses/capabilities.py` | 306 | `("codex", False)` 的 `PROACTIVE_SEND`、`TOOL_INJECTION` 改 `_native("app_server")`；167 行注释改为「the carrier delivers summaries; the model replies through the thread's harness-bridge MCP」 | ≤ 310 |

目录：`harnesses/codex/`、`shell/.../agent/runtime/` 已满 7，只改不增。测试新文件 `tests/daemon/harnesses/codex/test_codex_interactive_mcp.py`（该目录 6 → 7）。

## 3. 测试：`tests/daemon/harnesses/codex/test_codex_interactive_mcp.py`

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| Y1 | `test_interactive_session_mounts_harness_bridge` | 启动 interactive codex（假 app-server 记录请求）：按选定方案，`harness-bridge` 服务器出现在 thread resume config 或 app-server `config_args` 中，actor 为规范 URI | `AssertionError`：无该服务器 |
| Y2 | `test_interactive_thread_can_reply` | 线程内调 `harness_reply(id, text, summary)`：daemon 收到 `message.reply`，带 `replyTo`、合法 summary、当前 sessionRef；原请求闭合 | `AssertionError` |
| Y3 | `test_interactive_thread_can_read_body` | `harness_read_body(id)` 返回正文，行被盖 fetched 戳 | `AssertionError` |
| Y4 | `test_interactive_thread_can_ack` | `harness_ack(id)` 闭合不欠回复的行 | `AssertionError` |
| Y5 | `test_interactive_send_requires_summary` | `harness_send` 无 summary 得 `SUMMARY_REQUIRED`（WP3-B 前为参数校验或 WP6 工具面行为，按实施 base 实测写断言） | `AssertionError` |
| Y6 | `test_interactive_mcp_is_session_fenced` | 会话被取代后旧服务器的工具调用得 `SESSION_SUPERSEDED`/`STALE_SESSION`；方案 P 下 sessionRef 未写入前得 `STALE_SESSION` | `AssertionError` |
| Y7 | `test_native_capabilities_are_backed_by_a_tool_call` | `support("codex", headless=False, capability=PROACTIVE_SEND/TOOL_INJECTION).level is NATIVE`，且同一测试内 Y2 的路径成立（能力声明不得脱离工具调用证明单独为真） | `AssertionError`：level 为 unsupported |

真实 codex TUI 里模型实际调用工具（含审批策略 `-a never` 与 `on-request` 两种）的联测：本包内做第 0 步探测；完整用户路径记 BLOCKED-on-WP8，不由 unit 代替。

## 4. 验收（可机检）

1. `PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/harnesses/codex -q` 通过；Y1–Y7 七个名字都存在（`grep -c "def test_" tests/daemon/harnesses/codex/test_codex_interactive_mcp.py` ≥ 7）。
2. wp5.md §8 第 3–5 条同样执行。
3. `grep -n "PROACTIVE_SEND: _unsupported\|TOOL_INJECTION: _unsupported" src/hyprial/daemon/impl/harnesses/capabilities.py` 的输出中不含 `("codex", False)` 块内的行（以 `sed -n '/("codex", False)/,/},/p' src/hyprial/daemon/impl/harnesses/capabilities.py | grep -c "_unsupported"` → `0` 核）。
4. `grep -n "auto-reply" src/hyprial/daemon/impl/harnesses/capabilities.py` 无输出。
5. `git diff --name-only <base>..HEAD -- src/hyprial/daemon/impl/harnesses/codex/settlement.py src/hyprial/daemon/impl/harnesses/carrier src/hyprial/daemon/impl/mcp/server.py src/hyprial/kernel src/hyprial/daemon/impl/links src/hyprial/daemon/impl/inbox` 无输出。
6. `docs/evidence/org-structure-constrain-2026-10-14/wp5b/probe.txt` 存在，写明选定方案、codex 版本、审批策略与原始事件行；`blocked.txt` 列出真实 TUI 未实测项。
