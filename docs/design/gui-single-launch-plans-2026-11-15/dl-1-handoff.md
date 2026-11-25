# 交接：DL-1 —— `hyprial gui` 启动时自动拉起未运行的 daemon

日期：2026-11-15。发起：GUI 单一启动线。状态：已完成（见文末“由 GUI 线完成”一节）。

## 要解决的问题

对一个**已登录（有 owner）**的 home，如果 daemon 没有在运行（重启过机器，或上次经 GUI 完成初始化后关了窗口），
今天 `hyprial gui --no-open --json` 的结果是：

```json
{"ok":false,"code":"GUI_DAEMON_UNAVAILABLE","error":"the selected daemon is not reachable and ready","data":{}}
```

退出码 1（KP-064；[daemon 先行设计](../daemon-first-init-login-decoupling-2026-11-15.md) 的 G6 与三态表“状态 3”）。
DL-1 之后：`hyprial gui` 在准入之前先确保 daemon 在运行，然后正常打开。

owner 已裁定把 DL-1 计入 GUI 线的第一个达标点（“wheel 安装的 `hyprial gui` 可用”）。

## 范围

| home 状态 | 今天 | DL-1 之后 |
|---|---|---|
| 有 owner，daemon 在运行 | 正常 | 不变 |
| 有 owner，daemon 未运行 | `GUI_DAEMON_UNAVAILABLE` | 先启动 daemon，再准入 |
| 无 owner | 首次使用流程 | **不变，不调用**（Z 线处理） |
| driver 为 `contract-mock` | 不需要 daemon | **不变，不调用** |

实现要点（设计文档 §6 的 DL-1 行；更早的备忘见同目录 `daemon-lifecycle.md`）：

- 改动落在 `src/hyprial/shell/impl/cli/commands/gui/commands.py`：准入前调用既有的唯一启动原语
  `_launch_daemon_process`（`cli/commands/common/daemon_start.py`，经 `services` 注入，和 `daemon start`、`init`、`login` 的调用方式一致）。
  不新写启动逻辑，不新增 CLI 命令或参数。
- 启动失败时把 `CliError` 的 code 与 message **原样**映射进 GUI 启动结果（`--json` 时 stdout 恰好一个 JSON 对象且含 `ok`，`ok:false` 退出 1）。
- 关闭 GUI **不得**停止由此启动的 daemon。
- 预计约 45 行源码、约 200 行测试。

## 测试（先失败再通过）

1. 红测试：隔离 home、有 owner、无 daemon → 今天得到 `GUI_DAEMON_UNAVAILABLE`；改后 daemon 被启动且准入通过。
2. daemon 已在运行 → 不重复启动（只产生一代 daemon）。
3. 无 owner → 不调用启动原语。
4. `contract-mock` → 不调用启动原语。
5. 启动原语抛 `CliError` → 启动结果携带同一 code 与 message，退出码 1。
6. 至少一条真实进程的集成用例：隔离 home 上真实执行 `hyprial gui --no-open --json`，随后确认 daemon 存活；GUI 进程结束后 daemon 仍存活，再由测试显式停止并清理。

放在 `tests/shell/gui/` 下合适的子目录（每目录 ≤ 7 个直接文件，每文件 ≤ 1000 行）。

## 与其他在途工作的关系

- **PR 2c**（wheel 改走 Python host，GUI 线进行中）改的是 `shell/impl/gui/backend/**` 与 `shell/impl/gui/install/**`，预计不碰 `cli/commands/gui/commands.py`。
  本任务从当前 `dev` 开发即可；若 2c 先合入并产生冲突，以普通 merge 同步 `dev`。
- **CLI → daemon request 14 组集成**在改 `cli/commands/**` 的其他命令组与 `common/support.py` 的请求函数。不要改 `support.py`；只读地使用 `services`。
- **Z 线**之后会去掉本任务里“无 owner 不调用”这一个条件；因此请把该条件写成一处清晰的判断，不要散落。

## 红线

- 绝不读取、改写、启动或停止真实 `~/.hyprial` 下的 daemon 或真实 OS 服务；home、socket、端口全部隔离。
- 不改 `request_http/**`、`http_contract/**`、kernel IPC、`shell/impl/gui/**`、`gui/**`、`contract/gui-session/**`。
- CLI 输出只经 `shell/impl/cli/output`（返回 `CliResult`，不直接 print）。
- 不新增行内注释（决策写文件头决策块，docstring 保留）；不用 skip/xfail；一律 `uv`，Python 3.12。
- 若错误码或 JSON 字段语义发生变化，同步 `contract/`，否则在 PR 正文写 `Contract-Impact:` 说明。
- 更新 `docs/reference/known-problems.md` 的 KP-064：只更新原条目的状态，不删除。

## 验证命令

```bash
uv sync --extra test --python 3.12
uv run pytest -q tests/shell/gui
uv run ruff check .
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run --with libcst==1.9.0 pytest tests/
```

全量 pytest 的失败项请对照 `docs/reference/dev-red-baseline-2026-11-15.md` 归因，写进 PR 正文。

## 完成时请这样交回

1. 普通提交并推送到本 PR 的分支 `feat/gui-ensure-daemon`（不 force-push）。
2. 去掉 PR 标题里的 `WIP:`。
3. 在本 PR 评论写明“已完成”，列出：改动文件、各验证命令的真实结果、未完成或未测项（标 UNKNOWN / BLOCKED）。

GUI 线以“标题去掉 WIP + 完成评论”作为接手审查与合入的信号。合入由 GUI 线执行。

## 由 GUI 线完成（2026-11-15，接手后）

交接后的改动（接手者为 GUI 线；上文保持原状）：

- 钩子位置：CLI 文件不再读 `HYPRIAL_GUI_BACKEND_DRIVER`，也不再判断 owner；`commands.py` 只把启动原语作为
  `ensure_daemon` 传给 `launcher.build_plan`，由 `admit_source` 的 daemon 分支在身份、principal、socket 校验之后、
  `owner is not None` 时调用。driver 规则只在启动器中：安装态未设置即 `daemon`，`contract-mock`、空值、无效值不启动。
- 顺序：非法 action/`--ip`/`--port`、driver、`--gui-home`、平台预检、身份与 socket 校验的拒绝都发生在启动 daemon 之前
  （钩子在 `admit_source` 内部、这些校验之后）。源码模式下准入先于源码发现与项目检查（既有测试
  `test_*_admission_rejects_before_discovery_or_*` 固定了这一顺序），所以项目检查失败（如缺 `node_modules`）发生在
  daemon 启动之后；daemon 保持运行，下次启动复用。
- “恢复中”：启动原语的就绪是 socket 已绑定且 `ping` 可答，不含 restore 结束，所以刚启动的 daemon 会被准入判为
  `GUI_DAEMON_NOT_READY`。有 `ensure_daemon` 时，验证对该错误有界重试 60 秒（250ms 间隔）；没有钩子的准入行为不变。
- 测试：`tests/shell/gui/lifecycle/test_gui_daemon_ensure.py` 重写；真实进程用例 `test_gui_relaunch.py` 改为真实
  `hyprial gui --no-open --json`（安装态资源树，driver 未设置与 `daemon` 两种），旧用例桩掉 `build_plan` 因而不再覆盖本路径；
  `test_gui_installed_live.py` 的等待改为 ping 就绪而非 socket 文件存在。
