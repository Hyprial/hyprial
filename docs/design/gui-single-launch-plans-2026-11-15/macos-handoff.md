# 交接：macOS arm64 上的验收、原生壳与打包

日期：2026-11-16。发起：GUI 单一启动线。状态：进行中（WIP）。对象：有 macOS arm64 机器的独立开发者。

## 为什么需要一台 Mac

GUI 单一启动线到目前为止的所有实测都在 Linux x64 上完成。下列各项在 macOS 上的状态全部是 UNKNOWN，
而且其中一部分（原生壳的 Tauri crate、`.app` / `.dmg` 打包、launchd）在 Linux 上根本无法构建或运行：

- 安装态与源码态的 `hyprial gui`（Python session host、自动拉起 daemon、`gui dev` 的 Vite 转发）在 macOS 上是否可用；
- 原生壳 #2840（PR 6b）的 Tauri crate 能否编译，WebView 行为是否符合设计；
- macOS arm64 安装包（PR 6c）；
- 每个 home 一个 OS 服务 #2830（Z-8a–c）的 launchd 实机验收。

## 必读（按此顺序，冲突时取前者）

1. [GUI 单一启动设计](../gui-single-launch-2026-11-14.md) 与同目录 [`README.md`](README.md)（含 owner 决定汇总）。
2. 同目录 [`shell-6.md`](shell-6.md)：原生壳与打包的计划。重点：§C（壳依赖的启动器契约）、§D.3（6b）、§D.4（6c）、
   §E.2（macOS 验收项 M1–M8）、§E.3（没有签名凭据时能做到什么）、§G.1（owner 决定与默认值）。
   计划写于 PR 2c / PR 3 之前，行号与部分前提已变化，以当前 `dev` 为准，差异写进 PR 正文。
3. 仓库根 `AGENTS.md`。
4. #2840 上 GUI 线的审查评论（2 个阻塞项、9 个应修项）。

## 分三段，按顺序做

### 第一段：已合入内容在 macOS 上的验收（本 PR）

只验证、只修 macOS 独有的缺陷。全部使用隔离 home。逐项报告 PASS / FAIL / BLOCKED。

| # | 检查 | 做法 |
|---|---|---|
| A1 | 安装态 `hyprial gui` | `uv run --no-project --python 3.12 --with playwright==<与本机 Chromium 匹配的版本> python scripts/verify/verify_installed_gui.py --wheel <macOS arm64 wheel> --evidence <文件>`。wheel 取自 CI 的 macOS arm64 构建产物，或按 `scripts/build/build-wheel.sh` 在本机构建（需要干净工作区、Node 24、Rust 1.93.1） |
| A2 | 安装态由 GUI 自己拉起 daemon | A1 的脚本是先启动 daemon 再启动 GUI。另做一次：隔离 home 已登录、daemon 未运行，直接 `hyprial gui --no-open --json`，确认 daemon 被拉起、GUI 退出后 daemon 仍在 |
| A3 | 源码态 `hyprial gui` | 源码 checkout 上 `hyprial gui --no-open --json`：浏览器打开 `launch.url`，Start 页渲染，`/daemon/*` 返回 200，SIGTERM 后子进程全部回收、daemon 存活 |
| A4 | 源码态 `hyprial gui dev` | 同上，另确认热更新 WebSocket 连上；SIGTERM 后没有残留的 vite / esbuild / node 进程；杀掉 Vite 进程组后 GUI 以 `GUI_ASSETS_EXITED` 结束并清理 |
| A5 | 相关测试 | `uv run --with libcst==1.9.0 pytest -n 2 tests/shell/gui tests/integration/request_http/onboarding`；`npm --prefix gui test` |

macOS 上需要特别留意的点：AF_UNIX socket 路径长度上限比 Linux 短（隔离 home 用短路径）；`/tmp` 实际是 `/private/tmp`；
进程组与 `SIGTERM` 的回收；文件锁；默认 shell 与 `PATH` 中的 Node / Rust 版本。

发现的缺陷：macOS 独有且修复点在 `src/hyprial/shell/impl/gui/**` 或测试里的，在本 PR 修，先写失败用例再修；
其余在 PR 评论里报告，不自行扩大范围。

### 第二段：原生壳 #2840（PR 6b）

#2840 在 Linux 上只验证了 launcher crate（`cargo test` 16/16），Tauri crate 因缺少 WebKitGTK 开发包从未编译过。

1. 在 #2840 的分支上以普通 merge 同步 `dev`（不 force-push，不改写历史）。
2. 修掉审查评论里的 2 个阻塞项：
   - `scripts/ci/check_structural_limits.py` 把旧的受检根替换成了 `clients/native`，应改为**并列增加**，并恢复被改写的测试；
   - 新增的 workflow 违反 `tests/tooling/ci` 的 CI 策略测试。
   9 个应修项逐条处理或说明。
3. 在 Mac 上第一次编译 Tauri crate，并完成 `shell-6.md` §E.2 的 M3–M7：
   能构建；启动冒烟（`launch.url` 在授权有效期内出现并被消费）；`kill -9` 应用后启动器经 stdin EOF 在 5 秒内退出、无孤儿进程；
   手工检查清单（Start 页、刷新保持会话、登录点击打开系统浏览器、外链打开系统浏览器、主框架不能被外部地址替换、
   第二次启动聚焦已有窗口、退出不留进程）；桥接探测（页面调用 Tauri 内部 `invoke` 必须被拒绝，调用成功是发布阻塞项）。

依赖：壳通过 `hyprial gui --no-open --json --port 0 --stdin-stop` 启动后端。`--port 0` 与 `--stdin-stop`
属于 **PR 6a（Python 侧，GUI 线正在做）**。6a 合入前，第二段里需要这两个参数的检查标 BLOCKED，先做第一段和阻塞项修复。

### 第三段：macOS arm64 安装包（PR 6c）

按 `shell-6.md` §D.4 的 6c-1 至 6c-6，在 #2840 合入后另开 PR：运行时锁文件、`stage_runtime.py`、`verify_runtime.py`、
只在 Mac runner 上运行且**从不发布**的候选 workflow、`packaging/clients/README.md`。

- 应用标识：bundle id `com.hyprial.hyprialos`，显示名 “HyprialOS”（owner 决定；计划正文里的旧名以此为准）。
- 应用使用**自己的** `HYPRIAL_HOME`，与默认 home 分开（daemon 要求包版本精确一致，且每个 home 只有一个锁持有者）。
- 签名：现阶段只做 ad-hoc 签名（`signingIdentity: "-"`）。Developer ID 签名与公证需要的凭据由 owner 配置为 runner secrets，
  仓库中只按名称引用，**不得写入仓库、日志或 PR**；没有凭据时相关项标 BLOCKED。
- 目标只有 macOS arm64 与 Linux x64；本交接只做 macOS arm64。Windows 不处理，已有的 Windows 测试与门禁不删除。

### 可选：#2830（Z-8a–c）的 launchd 实机验收

#2830 暂缓中，代码已交付，缺的是 Mac 上的实机验收。如有余力：以普通 merge 同步 `dev`
（注意 `docs/reference/known-problems.md` 的 KP 编号，`dev` 已用到 KP-080，冲突的条目需要改号），然后在专用的非默认 home 上
验证服务的安装、启动、停止、卸载。验收结果写在 #2830 的评论里。这一项不阻塞前三段。

## 红线

- **绝不读取、改写、安装、卸载、启动或停止默认 home（`~/.hyprial`）及其 OS 服务（`com.hyprial.hyprial.daemon`）**，
  也不碰真实凭据与用户级配置。所有运行都用专用的非默认 home（例如 `$HOME/.hz-mac-*` 这样的临时目录），测试后自行清理。
- 共享机器上不做需要重启的操作。
- 不用 skip / xfail、自动重试、删除测试、放宽断言、扩大 allowlist 或单纯延长 timeout 来变绿。BLOCKED 不是 PASS。
- 不改 `src/hyprial/daemon/impl/request_http/**`、`daemon/impl/http_contract/**`、`daemon/impl/request_catalog/**`、
  kernel `ipc_errors.py`、`shell/impl/cli/commands/common/support.py`、`contract/daemon-request/openapi.json`
  （CLI → daemon request 集成正在逐组改动）。确需改动时在 PR 评论里提出。
- 原生壳只是最简包装：0 个 IPC 命令，不另起 GUI、后端或 inbox，不复制页面。
- 不新增行内注释（决策写文件头决策块，docstring 保留）；一律 `uv`，Python 3.12；`docs/notes/**` 只追加。
- 仓库文件中不出现内网主机名、机器用户名或邮箱。
- 已发布字段语义变化要同步 `contract/`，或在 PR 正文写 `Contract-Impact:`。

## 完成时请这样交回

每一段单独交回，GUI 线逐段接手审查与合入：

1. 普通提交并推送（不 force-push）。第一段推到本 PR 的分支 `feat/clients-macos-package`；第二段推到 #2840 的分支；第三段另开 PR。
2. 该段完成后去掉对应 PR 标题里的 `WIP:`，并在该 PR 评论写明“已完成”，列出：改动文件、逐项 PASS / FAIL / BLOCKED
   与实际输出摘要、macOS 版本与芯片、未测项（标 UNKNOWN / BLOCKED）。
3. 需要 owner 或 GUI 线裁定的问题在 PR 评论里提出。

合入由 GUI 线执行，开发者不自行合入。
