# PR 6b 交接：最简原生壳（在 Linux 开发机上实现）

日期：2026-11-15。状态：交接给独立开发者；工作分支 `feat/native-shell-minimal`，
PR 保持 WIP 直到本文第 5 节的验收完成。审查与合并由 GUI 单一启动线负责。

依据（先读，按此顺序）：

1. [GUI 单一启动设计](../gui-single-launch-2026-11-14.md) §4（原生壳：删除后按最简封装重建）。
2. [原生壳计划](shell-6.md)：B.3（推荐形态与运行时序）、B.4、C（启动器契约）、D.3（6b 的九个步骤）、
   G（风险与 owner 决定）以及文末追加的 owner 决定。本文不重复这些内容，只写交接边界。
3. 仓库 `AGENTS.md`。

## 1. 原则与形态

owner 原则：client 只是最简化封装，client 启动与“用户安装 wheel 后用浏览器打开 GUI 页面”几乎完全一致。

壳只做一件事：运行 `hyprial gui --no-open --json`，读取 stdout 第一行的启动记录和它指向的
handoff 文件，在一个 webview 窗口里打开该 URL；退出时停止子进程。壳内不嵌入 Python，不实现
host，不持有任何会话、收件箱或身份，webview 内容不能通过壳获得任何能力。

形态（计划 B.3）：Tauri v2，一个窗口，没有 command、没有 capability、除 single-instance 外没有插件。
全部逻辑在一个**不依赖 Tauri** 的 crate `clients/native/launcher` 里，Tauri 部分
`clients/native/src-tauri` 只做接线（目标 ≤ 200 行）。

## 2. 范围：计划 D.3 的 6b-1 … 6b-9，外加 6b-0

| 步 | 内容 |
|---|---|
| 6b-0（新增） | 写出启动记录的契约文件 `contract/cli/gui-startup-record.v1.json`（schema + 示例）与 `contract/cli/README.md` 的说明，内容照计划 C.3。它是壳与 CLI 两侧共用的语料：本 PR 的 Rust 测试解析其中每个示例；CLI 一侧的符合性（`record_version`、`--port 0`、`--stdin-stop`）由 GUI 单一启动线在 PR 6a 里对着同一个文件实现。文件落地后如需修改，在 PR 里提出，不单方面改 |
| 6b-1 … 6b-4 | launcher crate：`record.rs`（解析与校验启动记录、失败行）、`handoff.rs`（读取 handoff 文件；类型的 `Debug`/`Display` 与所有错误都不得包含 grant）、`origin.rs`（主框架导航只允许记录里的 origin 与内置状态页；外部 http(s) 链接交给系统浏览器，其余拒绝）、`child.rs`（启动、读 stdout、停止：关 stdin → SIGTERM → 对进程组 SIGKILL）；用写到临时目录的假启动器脚本做集成测试 |
| 6b-5 | Tauri 接线：`tauri.conf.json`（无 windows、无 capabilities、无 plugins 配置、`withGlobalTauri: false`）、`main.rs`、静态状态页 `ui/index.html`、图标 |
| 6b-6 … 6b-8 | 让仓库的三道门认识新的根 `clients/native`：daemon-request 架构门（`Command::new(` 只允许出现在 `child.rs` 一处）、原生壳表面的静态测试（见计划 6b-7 的清单）、结构门（每目录 ≤7 个直接文件，排除 `target/`、`gen/`、`runtime/`） |
| 6b-9 | `.gitignore`、`AGENTS.md` 与 GUI 规范里关于原生壳的描述、CI：`cargo test`、`cargo fmt --check`、`cargo clippy -D warnings`（launcher crate） |

不在 6b：Python 侧改动（6a）、运行时打包与 macOS 包及签名（6c，需要 Mac 与凭据）、自动更新、
托盘、deep link、移动端。

## 3. owner 已定事项

- Bundle identifier：`com.hyprial.hyprialos`；显示名：`HyprialOS`。
- 打包目标：macOS arm64 与 Linux x64。Windows 本轮不做：不新增 Windows 分支或测试，
  遇到 Windows 特有的问题记录后忽略；仓库现有的 Windows 检查保持通过，不删除。
- app 使用独立的 `HYPRIAL_HOME`，不与 `~/.hyprial` 共用（由打包与启动参数决定，6b 不需要实现，
  但不要写死 `~/.hyprial`）。
- daemon 与窗口的关系按 [daemon 先行设计](../daemon-first-init-login-decoupling-2026-11-15.md)：
  关闭窗口只停止 `hyprial gui` 这个前台进程，不负责停止 daemon。壳不需要知道 daemon 的存在。

## 4. 开发方式与边界

- 启动器命令：环境变量 `HYPRIAL_SHELL_LAUNCHER`（开发与测试）优先，否则是包内运行时
  （6c 提供）。参数固定为 `gui --no-open --json --port 0 --stdin-stop`。
- **今天的 `hyprial gui` 还不接受 `--port 0` 与 `--stdin-stop`，输出里也没有 `record_version`**
  （6a 尚未实现）。自动化测试一律用假启动器脚本。想对着真实 CLI 手动冒烟时，写一个本地包装脚本
  （不提交）：去掉那两个参数、给第一行补上 `record_version: 1`、把停止信号转成 SIGTERM；
  冒烟结果在 PR 里如实写明是“经包装脚本”，不能当作 6a 完成后的验收。
- grant 只交给 webview，不写日志、不上状态页、不进错误信息。
- 不要碰：`gui/**`、`src/hyprial/shell/impl/gui/**`、`contract/gui-session/**`、
  `src/hyprial/shell/impl/request_http/**`（其它工作正在改）。需要它们配合的地方在 PR 里提出。
- Rust 代码同样遵守仓库规则：不写行内注释，决策写文件头；每个代码文件 ≤1000 行，
  每目录 ≤7 个直接文件。
- 依赖只用 crates.io 上的公开 crate，`Cargo.lock` 入库；不 vendor。

## 5. 验收

仓库内（Linux）：

- 每步先有失败的测试（6b-5 除外）；`cargo test -p hyprial-shell-launcher --locked`、
  `cargo fmt --check`、`cargo clippy -- -D warnings`。
- `uv run --with libcst==1.9.0 pytest tests/tooling/architecture tests/tooling/ci -q`
  （本地用 Python 3.12，与 CI 一致）；`scripts/ci/check_structural_limits.py`、
  `check_module_boundaries.py`、`check_daemon_request_architecture.py`、
  `check_inline_comments.py --strict`；`node --test scripts/ci/*.test.mjs`。
  目标分支上已知的固有失败见 `docs/reference/dev-red-baseline-2026-11-15.md`（随 #2833 合入）。
- 本 PR 不改 `src/hyprial`，不需要全量 pytest；若改了，就需要。

Linux x64 实机记录（写进 PR 正文，逐项 PASS / FAIL / BLOCKED，附命令与现象）：

1. `cargo build` 出可执行文件；用假启动器启动：窗口先显示状态页，再导航到启动 URL。
2. 启动器输出 `ok:false`、无输出退出、超时不输出：状态页分别显示对应的 code 与信息；没有残留进程。
3. 关闭窗口：子进程在 stdin 关闭后退出；忽略 EOF 与 SIGTERM 且带孙进程的假启动器，在预算内被整组清理。
4. 第二次启动：聚焦已有窗口后退出（single-instance）。
5. 导航守卫：页面内跳到其它 origin 被拒；`window.open` 到其它 origin 的 http(s) 链接由系统浏览器打开；
   `file:`、`javascript:`、`data:` 被拒。
6. 经包装脚本对着真实 `hyprial gui`（contract-mock driver，隔离的 home 与端口，
   绝不使用生产 daemon、凭据或日常的 `~/.hyprial`）：页面加载，会话建立，
   `SameSite=Strict` 的 cookie 生效，地址栏里的 `#grant=` 片段被清除，
   页面内 `/daemon/*` 请求成功。失败项原样记录（WebKitGTK 的行为是本项要测的对象）。
7. webview 内的页面拿不到任何 Tauri API（`window.__TAURI__` 不存在；`ipc:` 不可达）。

macOS（WKWebView、打包、签名、公证、App Translocation）不在本 PR 验证，全部写 UNKNOWN，
留给 6c。

## 6. 遇到请记录、不要自行决定

- WebKitGTK 与契约不一致的行为（cookie、`Origin` 头、`Sec-Fetch-Site`、片段处理）。
- 登录流程里 `window.open('about:blank')` 之后再设置地址的用法在壳里的表现（KP-056）。
- 每次启动端口不同导致 `localStorage` 清空的影响（GUI 目前把 Squire 身份存在那里）。
- `./VERSION` 不是 semver，壳的版本号如何取（计划的默认值：壳自己的 semver，从 0.2.0 起）。
