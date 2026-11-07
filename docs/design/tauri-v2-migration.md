# Tauri v2 桌面迁移任务书（Electron → Tauri，深模块 / 薄外壳）

状态：**规划交付，未实施**。本文不是“Tauri 迁移已完成”的声明；任何 Tauri
能力在实施线程真实构建、打包、探测通过之前一律按 UNKNOWN 处理。

- 规划基准 worktree：`/Users/alicesmith/Sync/2-engineering/HyprialOS/worktrees/harness-bridge-tauri-v2`
- 分支：`refactor/tauri-v2`；HEAD：`1ea20ee0a964f2349903a4bab2e014345fa92733`
- 真正交接基准：`docs/evidence/tauri-v2-prep-2026-10-02/source-handoff.json`
  （`treeSha256: 4487cd961fb0fca91162c8d52d0d1613ef4ff9746f7a1f1795cc1f64d46ce202`，
  235 个文件条目：104 新增 / 131 preimage，另 346 个 deleted 条目）。
  整个 `git diff HEAD` 包含承接本身；**Tauri 的 delta 必须对该 snapshot 计算，不是对 HEAD**。
- 规划证据：`docs/evidence/tauri-v2-prep-2026-10-02/planning-report.md`（本树 235/235 哈希复核、
  deleted 复核、上游官方文档可达性、耦合点行数与哈希）。

## 0. 目标与不可违背的架构原则

主要目标是**深模块架构 / 薄外壳**，不是补齐旧功能：

1. **一份 GUI**：`src/gui`（`@hyprial/gui`，六模块公共面 studio/session/host/client/presentation/transport
   + `product` 组合根 + `plugins/trusted-panel.mjs`）是唯一 GUI，对实施线程只读。
2. **一个 Node/Cordis product 宿主**：打包的真实 Node >= 24 runtime 以 sidecar 方式运行
   `product/server.mjs`。不把 Cordis/Node 逻辑重写进 Rust，不引入第二个常驻 Node 宿主。
   第一版直接打包官方 Node >= 24 发行物 + JS 资源（不承诺任何 single-binary 打包工具支持该
   版本；不用 pkg/sea 等工具伪造版本兼容）。
3. **一个 daemon 业务权威**：session/ACK/inbox/PAC 的业务权威只在 daemon；GUI 仅持有非权威展示投影，
   GUI Studio 设计文档由 GUI product 的既有持久化模块管理。Rust 外壳**不得复制**任何业务状态、
   第二套规则或 Studio 文档存储。
4. **Rust 只负责**：窗口、权限（capabilities）、受限 native IPC、sidecar/子进程控制与资源打包；
   **不对渲染层开放 generic spawn/shell/fs**。进程控制优先在 Rust（`std`/`tokio` process），
   渲染层只能调用显式枚举的 `#[tauri::command]`。
5. 系统 WebView（macOS WKWebView / Windows WebView2）**不是** Chromium Node runtime：
   Electron 时代 unit green 不能冒充 Tauri WebView 通过；浏览器差异必须在打包后的 Tauri 应用里验证。
6. 分层目录语义明确：每个 source-bearing 目录 ≤ 7 个 direct 文件、单文件 code ≤ 1000 行；
   `desktop/src-tauri/src` 在现有 shared gate roots 之外，gate 扩展与 Rust 真实 root 由主线以 IR 集成，
   worker/实施线程不得私改共享 gate，也不得把代码藏进 vendor/target 躲门。

上游官方参考（规划期已核对，HTTP 200，见 planning-report）：

- https://v2.tauri.app/learn/sidecar-nodejs/ （sidecar 可 bundle Node runtime 或单 binary；IPC 可 localhost/stdin/socket）
- https://v2.tauri.app/security/capabilities/ （capability 最小授权模型）
- https://v2.tauri.app/concept/architecture/ （进程/WebView 架构）

只引用上述官方 primary 文档；实施期若需进一步核对，只读 public curl，不执行远程安装脚本。

## 1. 现状：真实 Electron 耦合点与 keep/replace/drop/defer

以下全部基于本 worktree snapshot 实测（哈希/行数见 planning-report），不基于旧 DSH 历史。

| 耦合点 | 现状（snapshot 实测） | 处置 |
| --- | --- | --- |
| `desktop/src/main.cjs`（403 行） | Electron app 生命周期、单实例锁、startup/setup/account 窗口、主窗口加载 GUI URL（origin fence + 全 deny 权限）、菜单（首次设置/组件/账户/更新/回滚/诊断）、shell 更新（electron-updater，当前 `channel.json shell:null` 即未启用）、payload 更新、onboarding 自动应用、`--hyprial-smoke-test` 冒烟 | **replace**：拆为 Rust `app.rs`/`windows.rs`/`startup/smoke.rs` + 各 capability 窗口 |
| `desktop/src/gui/launcher.cjs`（171 行） | 用 bundled Node spawn `product/server.mjs`，显式 `HYPRIAL_GUI_*` 环境、`hyprial.gui-launch/v1` 0600 launch-info 回读、readiness stdout 行、token 脱敏诊断环、`stopChild` 有界 TERM(12s)→KILL | **replace 实现、冻结协议**：Rust `runtime/gui.rs` 重写，协议逐条保持（§3 冻结表） |
| `desktop/src/runtime/boot.cjs`（195 行） | DesktopRuntime：隔离 home、daemon socket、Node GUI、bounded dispose；仍存在 components.dsh 启动前置及 Windows ACL 对 DSH sandbox 模块的读取 | **replace**：Rust `runtime/` + `supervisor/` 模块组。解除 DSH 启动前置，Windows ACL 用桌面自身原生实现；保留 DSH 为可选 agent harness，不复制其 GUI/agent runtime |
| `desktop/src/runtime/paths.cjs`（56 行） | `manifest.json` v1/v2 唯一地址簿；core/full 发行各自 required entries；reserved `root/manifest/entryKinds`；realpath 越界拒绝 | **keep 语义**：Rust `runtime/manifest.rs` 等价移植（含全部反例） |
| `desktop/src/runtime/daemon-ipc.cjs`（39 行） | 换行分隔 JSON-RPC v1 over unix socket / Windows named pipe（pipe-v1 endpoint 文件），仅 `ps\|shutdown`，8MB 上限 | **keep 协议**：Rust socket/pipe client |
| `desktop/src/runtime/network/process.cjs`（111 行） | `network-worker.py` NDJSON 事件流（status/login/join/forward），POSIX detached 进程组 / Windows `taskkill /T /F`，120s/660s 超时，1MB stdout 上限，冷启动 status 重试一次 | **keep 协议与超时**：Rust `account/worker.rs` |
| `desktop/src/account/window.cjs`（46 行）+ preload/renderer | Electron 窗口 + `ipcMain.handle('hyprial-account')` sender/frame 校验、仅 https openExternal | **replace**：Tauri 独立窗口 + scoped command + opener（https-only 校验保留） |
| `desktop/src/onboarding/*`（window 96 / apply 90 / actions 53 行） | 窗口壳 + Electron-free 的 `apply.cjs`（spawn bundled CLI，JSON envelope）；规则全在 payload CLI | **keep 边界**：Rust `onboarding/cli.rs` 直接 spawn 同一 CLI argv；窗口换 Tauri |
| `desktop/src/setup/window.cjs` + `components/{catalog,policy,registry}.cjs` | core 发行组件配置窗口（node/dsh/codex/claude 扫描/下载/确认） | **defer**（§8）：交互式组件窗口暂后；既有构建工具可复用。新 GUI 只需 Node + GUI 资源；Python/CLI 是接入 daemon 的独立资源，DSH 仅可选 harness，不是 GUI 启动依赖 |
| `desktop/src/startup/*`（phases/phase-runner/进度窗口） | 可恢复 startup phase 记录（home 内持久化）+ 进度窗口 | **keep 语义**：Rust `startup/phases.rs` 保持记录格式兼容；窗口换 Tauri |
| `desktop/src/update/*`（payload-store 468 / payload-update 132 / shell 42 / actions 89 / channel.json） | channel index → payload manifest → sha256 校验下载/解压（extractor）、generations+pointer 换代、rollback、`autoUpgrade` 设置门；shell 更新当前未配置 | **keep payload 平面**：优先复用 Electron-free 单一逻辑，按实际耦合决定是否移植；electron-updater **drop** |
| `desktop/src/runtime/kanban.cjs`（217 行） | whl 安装 + identity-pending/ready 状态 | **keep**：Rust `onboarding/kanban.rs` 同语义 |
| `desktop/src/diagnostics.cjs` | 打开诊断目录（shell.openPath） | **replace**：Rust command + opener |
| electron-builder（asar、before/after-pack、NSIS/DMG、`identity:"-"` ad-hoc、notarize:false） | 打包与校验钩子（symlink 越界审计、asar 内 lockfile 校验、tsnet pinned hash + codesign verify） | **replace**：tauri bundler + 把 before/after-pack 审计移植为独立校验步骤；ASAR 概念消失 |
| `desktop/package.json`（electron 44.4.2 / electron-builder 26.15.3 / electron-updater 6.8.10） | Electron 依赖三件套 | **drop**（M7 删除；构建改用 desktop 内精确版本的 `@tauri-apps/cli` devDependency，配套 Cargo.lock。不得用浮动 `^2` 称作固定版本，也不安装全局 CLI） |
| `desktop/tests/*.test.cjs`（snapshot 基线 161 PASS） | Node 测试覆盖 launcher/boot/staging/update/onboarding wiring 等 | **replace 不是 skip**：逐文件映射（§9）；被移植逻辑的等价测试进 `cargo test`，构建脚本逻辑保留 Node 测试 |
| probe 脚本（probe-core/live-runtime/packaged-install/macos-dmg/windows-installer/business-network/peer-network 等） | 打包后探测，依赖 `--hyprial-smoke-test` 行为与 `smoke-result.json` 模式 | **keep 接口**：Tauri 二进制保持同 CLI flag 与 result schema；probe 路径按 Tauri bundle 布局适配 |

## 2. 目标深模块结构与权威/依赖方向

```
desktop/                                  # 实施线程唯一可写根
  src-tauri/
    Cargo.toml  Cargo.lock  build.rs       # 独立 crate（不建 workspace 牵连仓库根）
    tauri.conf.json                        # v2 配置：窗口、bundle、resources、显式 capabilities
    capabilities/
      main.json  startup.json  account.json  onboarding.json   # 每窗口最小授权
    icons/…
    ui/                                    # 静态渲染页（vanilla HTML/JS，无新前端框架）
      account.html  onboarding.html  startup.html  shared.js  shared.css
    src/
      main.rs            # 薄入口
      app.rs             # 生命周期/启动序列/状态接线（对应 main.cjs 的非窗口编排）
      error.rs           # 命名错误码（保持 GUI_PRODUCT_CONFIG / DESKTOP_GUI_CONFIG 等字面）
      logging.rs         # 子进程诊断环 + token 脱敏
      windows.rs         # 全部窗口创建、origin fence、单实例、relaunch
      runtime/           # mod.rs manifest.rs env.rs daemon.rs gui.rs
      supervisor/        # mod.rs owned.rs shutdown.rs   # 仅 owned 子进程；有界 TERM→wait→KILL；reap
      account/           # mod.rs worker.rs             # network-worker 协议客户端
      onboarding/        # mod.rs cli.rs kanban.rs
      update/            # mod.rs channel.rs payload.rs store.rs
      startup/           # mod.rs phases.rs smoke.rs
      ipc/               # mod.rs account_cmds.rs onboarding_cmds.rs update_cmds.rs
```

依赖方向（只允许向下、单向）：

```
ui(渲染页) ──invoke──▶ ipc(command 枚举) ──▶ app ──▶ { runtime, supervisor, account, onboarding, update, startup, windows }
runtime/supervisor ──▶ 子进程协议边界（GUI product / daemon / network worker / CLI）
任何模块不得依赖渲染层；任何渲染层不得获得 spawn/shell/fs 能力
```

权威划分：

- **frontend 权威** = `src/gui` product（SSR shell + 资产 + token 会话），由 bundled Node 宿主运行。
- **backend 业务权威** = daemon（Python）+ `src/hyprial`（主线重构中，本树只按 manifest 消费）。
- **外壳权威** = Rust：进程监督、窗口、权限、打包资源。外壳不持有业务状态；
  启动期进度/代次指针等**簿记**（startup phases、last-startup/last-shutdown、payload generations）
  属外壳簿记文件，格式与今日语义保持兼容。

结构纪律自检（仓库根执行；递归扫描 source-bearing 目录，direct 文件计入该目录的**全部普通文件**，
不把 `wc` 总计当单文件；不允许机械编号分片、空目录或压缩代码绕门）：

```sh
uv run --frozen python - <<'PY'
from pathlib import Path
import sys
suffixes = {'.rs', '.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.html', '.css'}
problems = []
for root in map(Path, ('desktop/src', 'desktop/src-tauri/src', 'desktop/src-tauri/ui')):
    if not root.is_dir():
        continue
    for directory in (root, *(p for p in root.rglob('*') if p.is_dir())):
        files = [p for p in directory.iterdir() if p.is_file()]
        code = [p for p in files if p.suffix in suffixes]
        if code and len(files) > 7:
            problems.append(f'DIRECT-FILES {directory}: {len(files)} > 7')
        for file in code:
            lines = len(file.read_text(encoding='utf-8').splitlines())
            if lines > 1000:
                problems.append(f'TOO-LONG {file}: {lines} > 1000')
print('\n'.join(problems) if problems else 'STRUCTURAL PASS')
sys.exit(bool(problems))
PY
```

## 3. 冻结接口表（共享 freeze；实施线程不得更改生产者/协议）

| # | 接口 | 冻结内容 | Tauri 侧要求 |
| --- | --- | --- | --- |
| F1 | GUI 入口 | `src/gui/product/server.mjs`（sha256 `66f276bf…1a52`），包名 `@hyprial/gui`，`engines.node>=24`，`hyprial.install/v2`（`hyprial-install.json` sha256 `16020448…39ea`） | Rust 以 bundled Node 运行同一路径；不改 GUI 任何字节 |
| F2 | 显式配置环境 | `HYPRIAL_GUI_TRANSPORT_DRIVER`（必填）、`HYPRIAL_GUI_PRINCIPAL_ID`（或本机 `hyprial login` 记录 owner 兜底）、`HYPRIAL_GUI_TRANSPORT_CONFIG`（可选 JSON）、`HYPRIAL_GUI_STUDIO_ROOT`、`HYPRIAL_GUI_PORT`、`HYPRIAL_GUI_TOKEN`、`HYPRIAL_GUI_LAUNCH_INFO_FILE`、`HYPRIAL_GUI_ASSETS_DIR` | `runtime/env.rs` 逐名透传；缺 driver/principal → 命名错误 fail-closed，**绝不 fallback mock** |
| F3 | 启动回执 | `hyprial.gui-launch/v1`：0600 原子写 `{schema,url}`；stdout 仅打 origin：`hyprial gui: listening on <origin> (token URL in <file>)` | Rust readiness 轮询解析同一行 + 回读回执文件并校验 mode 0600 与 origin 一致；token 不落日志 |
| F4 | 进程关停纪律 | 父进程已 owned 子进程：有界 `TERM → wait(≤12s) → KILL → reap`；daemon 先 IPC `shutdown`(≤10s) 再升级；**unknown PID 一律不 kill** | `supervisor/` 只登记自己 spawn 的子进程；kill 只针对 owned 句柄 |
| F5 | loopback readiness | GUI/网络端口仅 `127.0.0.1` 预留-释放-使用；daemon socket 路径 macOS <104 字节 | `runtime/gui.rs`/`env.rs` 保持 |
| F6 | runtime 地址簿 | `manifest.json` schemaVersion 1\|2、`entries`+`entryKinds`、reserved 名与路径/类型校验 | 兼容读取既有 manifest；新 GUI 启动最小资源是 Node+GUI，daemon 接入另外需要 Python/CLI，不以可选 DSH 是否存在判 GUI readiness。desktop producer/consumer 的 required 集调整同批修改并有反例测试，不改冻结 GUI/Python 生产者 |
| F7 | daemon IPC | NDJSON v1、仅 `ps\|shutdown`、8MB 上限、Windows pipe-v1 endpoint 文件校验 | Rust client 同约束 |
| F8 | network worker | `network-worker.py` NDJSON 事件 + result；action ∈ status\|login\|join\|forward；超时 120s/660s；1MB stdout 上限 | `account/worker.rs` 同协议；POSIX 进程组 / Windows taskkill 语义保留 |
| F9 | bundled CLI argv | `python -I -S -B <bundled-cli.py> …`（daemon/onboarding 等全部经它） | Rust spawn 同 argv 形状 |
| F10 | 冒烟契约 | CLI flags `--hyprial-smoke-test`、`--hyprial-smoke-hidden`、`--rollback-payload`、`--configure-account`；`smoke-result.json` 字段 `platform/packaged/guiMounted/guiQueries/guiDriver/kanban/shutdown{host,daemon,forced}/status`；mock 必须**主动**选择（smoke 显式设 `mock-fixtures` 并断言 `guiDriver==='mock-fixtures'`） | Rust `startup/smoke.rs` 产同 schema 同 flag；mock 显式声明纪律不变 |
| F11 | payload 更新平面 | channel `update-index.json` → payload manifest（sha256 条目）→ generations + committed pointer + rollback；`autoUpgrade` 由 runtime-home `hyprial/settings.json` 门控；失败不改指针 | Rust `update/` 保持单源真理（不复述 artifact 清单） |
| F12 | 身份边界 | 浏览器输入不是受信身份；账户/网络身份只经 account 能力（worker）或本机 `hyprial login` 记录 | ipc 层不提供任何“由页面声明 principal”的命令 |

## 4. 实施线程精确 read/write/deny 表

**write（仅此 worktree）**：`desktop/**` 全部（含新增 `desktop/src-tauri/**`、`desktop/package.json`、
`desktop/scripts/**`、`desktop/tests/**`、`desktop/*.md`），以及实施线程自己的证据目录
`docs/evidence/tauri-v2-prep-2026-10-02/impl/`（不得覆盖 `source-handoff.json` 与规划文件）。

**read**：整树只读可用，特别是 `src/gui/**`（单一 GUI，哈希对照 handoff）、`src/hyprial/**`、
`pyproject.toml`/`uv.lock`/`uv.toml`、CI 配置、`/private/tmp/hyprial-joint-refactor.Zv6898/integration`
（只读源，供 snapshot 复原参照）。

**deny**：

- 写 `src/gui/**` 的源码、锁文件与既有输入（仅 §7 明列 ignored 安装/构建产物例外）、`src/hyprial/**`、
  `src/**` 其余、仓库根 locks/CI/`.agents`/`skills`、
  主迁移树与 integration 树任何字节。
- `git commit/push/reset/stash`（handoff `integrationPolicy`：no auto commit/push/cherry-pick；
  集成由主线单点完成）。
- 私改共享 gate/CI 以纳入 Rust；一切共享需求走 IR（§5）。
- 读取用户真实凭据/生产 daemon/生产 config；不复制 auth。模型 API 认证属运行设施，不得打印。
- 伪造 stamp/commit/发布声明；不得宣称“原生打包 PASS”除非本机真实构建+探测通过。

## 5. IR 清单（发给主模型，由主线单点集成）

| IR | 内容 | 触发时机 |
| --- | --- | --- |
| IR-1 | shared structural gate 扩展：把 `desktop/src-tauri/src`（+ `ui`）纳入 roots，规则同 ≤7 direct files / ≤1000 lines / .rs 纳入扫描 | M1 结束前发出（gate 生效前实施线程用 §2 自检脚本自律） |
| IR-2 | CI：Rust toolchain provision（对齐 `ci-ensure-uv.sh`/`ci-ensure-go.sh` 模式）、`cargo fmt --check`/`clippy -D warnings`/`cargo test` job、`desktop/src-tauri/Cargo.lock` 入库策略 | M1 |
| IR-3 | 发布流水线：tauri bundler 产物（.app/.dmg/NSIS）替换 electron-builder 产物、download-site/release manifest 适配、DMG/安装器探测纳入 CI | M6 前 |
| IR-4 | `src/gui` 漂移：handoff 之后 src/gui 任何字节变化 → 停止并上报，不自动追 main | 任何时候 |
| IR-5 | 仓库级 Electron 引用清理（根文档/CI scope/共享脚本中对 desktop electron 测试的引用） | M7 后主线收拢 |
| IR-6 | Windows 交叉构建与签名基础设施（本机为 macOS arm64，win-x64 构建在本机 BLOCKED） | M6 |
| IR-7 | shell（应用自身）更新通道决策：今日 `channel.json shell:null` 即未启用；Tauri updater 插件是否启用是独立后续变更，不在本迁移范围 | M7 报告中记录边界即可 |

## 6. baseline 与漂移 / 收拢策略

**基线冻结（M0，实施线程开工第一件事）**：

```sh
cd /Users/alicesmith/Sync/2-engineering/HyprialOS/worktrees/harness-bridge-tauri-v2
git rev-parse HEAD   # 必须仍是 1ea20ee0a964f2349903a4bab2e014345fa92733
# 对 source-handoff.json 全部 235 文件逐一 sha256、346 个 deleted 一一缺席
# （校验脚本照 planning-report §1 复制进 impl/）
```

任何不一致 → 停止并上报（可能 snapshot 被污染）。

**src/gui 漂移守卫**：每个里程碑结束时对 58 个 `src/gui/**` handoff 条目重跑哈希；不一致 → 停、
发 IR-4、不自动合并 main、不把旧 Python snapshot 当新 published runtime。

**desktop-only delta 记录（M7 交付物）**：对 `desktop/**` 相对 snapshot 的每处变化输出
`docs/evidence/tauri-v2-prep-2026-10-02/impl/desktop-delta.json`：
`[{path, op: added|modified|deleted, preimageSha256(handoff 值或 null), postSha256, mode}]`。
主线核对 current-preimage 后单点决定采纳或明确拒绝冲突；`git diff HEAD` 不是 Tauri 的 delta。

**回滚**：只在本树。M0 首先将 handoff 对应的 desktop 原字节/文件 mode 保存为本线程不可覆盖的
`impl/checkpoint/`，逐文件哈希核验。只从这个冻结 checkpoint 恢复自己的改动或反向套用可验证 delta；
integration 是仍在推进的只读源，只有字节哈希仍匹配 handoff 时才能作为补充来源，不能默认它永远不变。
新 GUI/desktop overlay 不在 HEAD 中，Git history 不能代替 checkpoint。
**禁止 `git reset --hard`**（会抹掉整个未提交承接 overlay），禁止触碰主线/integration。
整体放弃 = 留下 worktree 与报告。

## 7. 里程碑任务段（连续自主执行；仅在漂移/IR 依赖/真实 BLOCKED 时停）

每段自带验证与修复回路：失败 → 修复 → 重跑全段验证；不允许 skip。通用验证命令：

```sh
(cd desktop && node --test tests/*.test.cjs)                    # 每条均从仓库根执行
cargo fmt --manifest-path desktop/src-tauri/Cargo.toml -- --check
cargo clippy --manifest-path desktop/src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path desktop/src-tauri/Cargo.toml
npm --prefix src/gui run verify
# git status 已含承接 overlay；不是零改动证明。只读证明是逐文件 handoff 哈希/模式核验。
```

GUI 验证命令允许以 `npm --prefix src/gui ci` / `build` 形式在**本专用树**运行：只会在 src/gui 内
生成被忽略的 `node_modules`/`dist`，GUI 源码与 lockfile 始终以 handoff 哈希冻结为准（§6 守卫），
不修改共享面或主线产品。

**M0 基线冻结与证据**（无代码）：跑 §6 校验；记录 pre-state 摘要；建 `impl/` 证据目录。
出口：235/235 OK + deleted 缺席 + HEAD 复核通过 + desktop 原字节 checkpoint 核验通过。

**M1 脚手架**：在 `desktop/src-tauri` 建 crate；Tauri CLI 使用 desktop 内核对过的精确版本依赖，
提交待审的 package-lock/Cargo.lock 变更，不安装全局 CLI；`tauri.conf.json`：identifier
`org.hyprial.desktop.prototype`、macOS arm64 dmg + Windows x64 nsis 目标、`signingIdentity "-"`
（对齐今日 ad-hoc）、notarize 关、resources 声明 runtime 资源。
不向 loopback GUI 窗口默认开放全局 Tauri/native API；`withGlobalTauri` 不是权限隔离替代品，
`core:default` 也不能自动称为最小授权。可信本地壳页面按窗口显式枚举所需权限，主 GUI 保持无
generic native IPC；确需远程窗口命令时只能当次精确 origin + 指定 window + 有限命令白名单。
注意官方默认 registered app commands 可被所有窗口调用，必须通过 AppManifest/权限声明及
命令自身 sender/window 检查收紧，不能只写 capabilities 文件就认为已限制。
参考：[官方 capabilities / app commands](https://v2.tauri.app/security/capabilities/)。
单实例插件；主窗口 navigation handler 仅放行当次 GUI origin。
**注意**：Tauri v2 对自定义 app data 目录的支持需在官方 API 文档现场核对（候选：Builder 级 data
directory 覆盖以延续 macOS `~/.hyprial-desktop-prototype` 语义）；若不支持，采用平台默认
`app_data_dir` 并在证据中记录路径映射与 diagnostics/probe 适配。
出口：`cargo tauri dev` 起窗口；fmt/clippy/test 绿；IR-1/IR-2 已发。

**M2 runtime 核心**：移植 `manifest.rs`（F6 全反例）、`env.rs`（隔离 home、Windows SystemRoot 校验、
daemon socket 长度）、`supervisor/`（F4：owned 注册、有界关停、reap、unknown-PID 拒绝测试用
stubborn-child fixture）、`daemon.rs`（F7）、`logging.rs`（token 脱敏，反例：`?token=` 注入）。
出口：`cargo test` 含 §10 安全反例 S5–S8、S10–S11 全部转绿。

**M3 GUI sidecar**：`gui.rs` 按 F1–F5 spawn bundled Node + `product/server.mjs`；readiness 行 +
launch-info 0600 + origin 校验；主窗口加载 token URL；smoke HTTP 边界（shell + authed JSON）移植；
`guiMounted` 探测须证明**真实 WebView 挂载**（可尝试受限脚本/窗口探测，先在 WKWebView 实测）。
HTTP 成功只能证明 HTTP 边界；挂载探测做不到时标 UNKNOWN/BLOCKED，不能回填 `guiMounted=true`
或把 HTTP 成功冒充 WebView 验收。不要为探测给业务 GUI 开放泛化 native bridge。
出口：fail-closed 反例 S1–S4 绿；`--hyprial-smoke-test`（mock 显式）产 schema 兼容 smoke-result.json；
开发态可用显式外部 Python 发行物或离线 mock 驱动（Python 重构由主线完成后集成，不伪造 stamp）。

**M4 窗口与能力边界**：startup 进度窗、account 窗（`account/worker.rs` + https-only opener + 进度事件）、
onboarding 窗（`onboarding/cli.rs`）、diagnostics、菜单等价物、relaunch（`--configure-account` 等旗标保留）；
capabilities 每窗口最小集；ipc 层 sender/window 校验。
出口：反例 S9、S12 绿；`probe-account-window`/`probe-onboarding-window` 适配后 PASS。

**M5 更新与回滚**：F11（check/apply/rollback、autoUpgrade 门、--rollback-payload 旗标、startup phase
记录）保留单一实现。Electron-free 的既有 Node 逻辑可以直接复用为受限的短生命周期工具，
不要求为换壳全部重写 Rust，不新增第二个常驻 GUI 宿主；只有真实 Electron 耦合才替换。
对应既有测试可保留；真正移植的逻辑以等价 Rust 测试替换（映射表 §9），回滚坏代次/无前代反例保留。

**M6 打包与探测**：打包真实 Node >=24 + 单一 GUI 资源，正常 daemon 集成消费显式 Python/CLI 资源；
DSH 仅是可选 agent harness，不成为 GUI 或 Tauri 必需项。不要为此修改冻结的 GUI/后端接口。
`build-release.cjs` 改走 tauri bundler，移植 before/after-pack 审计为独立
校验步骤（symlink 越界、operator 文件禁入、tsnet pinned hash + `codesign --verify` 对齐
`verify-macos-signature.cjs` 语义）；GUI producer inputs 校验（`gui-product-inputs.cjs`）保留复用。
探测适配：`probe-live-runtime`、`probe-packaged-install`、`probe-macos-dmg`、`probe-business-network`、
`probe-network-status`。win-x64：本机 BLOCKED（IR-6），只允许 UNKNOWN 标记 + 命令就绪，不得声称 PASS。
出口目标（macOS arm64）：真实构建 + 打包后显式 mock 冒烟 + 原生窗口探测，分别报告实测。
若发布 provenance/新 Python 发行物尚未由主线提供，将相应发布闭环列 BLOCKED，继续完成可隔离
证明的 Rust/Node/GUI/打包层检查；不得使用基线旧 Python 或假 stamp 冒充新结构发行验收。
全部证据入 `impl/`。

**M7 清理与交接**：删 Electron 三依赖与 `desktop/src/**` Electron 专属文件（main.cjs、各 window/preload/
renderer、launcher.cjs、boot.cjs 等——被 Rust 替代者）；`desktop/package.json` 只剩构建工具依赖；§9
测试映射表闭合（无 skip）；§2 结构自检绿；产出 `desktop-delta.json` 与收拢报告（含 §8 边界与
UNKNOWN 清单）。不 commit。

## 8. 功能边界（不扩 feature；可暂后项显式记录）

- **defer**：交互式组件配置窗口（core 发行 UI）；首批 Tauri v2 不补齐组件下载界面。
  缺必要资源时 fail-closed 并给出命名错误；这是显式能力边界，不把不支持的 core 发行报成功。
  `probe-core` 可验证拒绝行为，但报告必须区分“拒绝测试 PASS”和“core 功能未实现”。
- **defer**：shell 自更新（现状即 `shell:null` 未启用；IR-7）。
- **defer**：notarization/hardenedRuntime（对齐今日 ad-hoc 内部通道）。
- **不做**：托盘/开机自启/新 GUI 功能/第二套会话或 inbox 逻辑/Rust 重写 Cordis。
- **开发期 Python**：允许显式外部 Python 发行物或离线 mock 驱动测试；真实 Python 发行集成由主线
  完成后统一发布，本树不复制不伪造。

## 9. 旧测试替换映射（replace，不是 skip）

| Electron 时代测试 | 替换 |
| --- | --- |
| `gui-launcher.test.cjs` | `runtime/gui.rs` 的 Rust 集成测试（同用例：fail-closed 配置、launch-info 回执、脱敏、关停纪律） |
| `runtime-boot.test.cjs` | `runtime/`+`supervisor/` Rust 测试（env/manifest/daemon readiness/dispose 报告） |
| `daemon-ipc.test.cjs` | `runtime/daemon.rs` 测试（协议反例同源） |
| `network-profile.test.cjs` / account wiring | `account/worker.rs` 测试 + `probe-account-window` |
| `onboarding-*.test.cjs`、`first-run-apply.test.cjs` | `onboarding/cli.rs` 测试（envelope 语义不变） |
| `payload-store`/`payload-update`/`update-actions`/`payload-update-channel`/`shell-updates` `.test.cjs` | 复用逻辑保留 Node 测试，真实移植者用等价 Rust 测试替换；shell-updates 保留“未配置”行为断言，不为换壳启用 updater |
| `startup-phase-*`/`startup-phases.test.cjs` | `startup/phases.rs` 测试（记录格式兼容用旧 fixture） |
| `kanban.test.cjs` | `onboarding/kanban.rs` 测试 |
| `components`/`component-policy`/`pack-component`/`gui-product-inputs`/`python-*`/`bytecode-cache`/`packaged-assets`/`tree-digest`/`download-server`/`feedback` | 构建工具侧保留 Node 测试（不随 Electron 死）；components 交互窗相关用例标记 defer 边界并改断言 fail-closed |
| 打包探测（probe-*） | 直接适配 Tauri 产物布局后继续作为验收 |

基线：snapshot 时代 desktop 161/161 PASS（integration VALIDATION，Node 26.7.0 实测）；
迁移完成态的 desktop 测试总数允许变化，但**每个被删/改测试必须在映射表有一行**，不允许静默 skip。

## 10. 安全失败反例（必须在测试中可证明）

- S1 缺 `HYPRIAL_GUI_TRANSPORT_DRIVER` → 命名错误，无 mock fallback。
- S2 无 principal（无 env 且 settings.json 无 owner）→ fail-closed。
- S3 smoke 未显式选择 mock → 失败；`guiDriver` 与声明不符 → 失败。
- S4 launch-info 文件 mode ≠ 0600 或 schema/origin 不符 → 拒绝。
- S5 manifest entry 保留名/越界/类型不符 → 拒绝。
- S6 supervisor 对未 owned PID 的 kill 请求 → 拒绝（API 层不存在该路径）。
- S7 子进程无视 TERM → 12s 后 KILL 且 reap（无僵尸）。
- S8 诊断流含 `?token=…` → 日志脱敏。
- S9 渲染层调用未注册命令 / 尝试导航外来 origin / window.open → 拒绝（capabilities + fence）。
- S10 daemon IPC 垃圾帧/超 8MB/版本不符 → 有界拒绝。
- S11 account open URL 为 http/带凭据 → 拒绝。
- S12 任何 capability 文件出现 shell/process/fs 授权 → 构建期自检失败（desktop Node 测试守卫 `capabilities/*.json` 白名单）。

## 11. 平台 / 签名 / 真实 backend 纪律

- 平台矩阵：macOS arm64（本机可真实构建+探测）；win-x64 本机 **BLOCKED**（IR-6），一切 win 结果只能
  UNKNOWN，禁止凭单测冒充原生包 PASS。
- 签名：ad-hoc（`signingIdentity "-"`、无 notarize）与今日对齐；tsnet pinned hash + codesign verify
  语义保留为独立校验步骤。
- 真实 backend：GUI `npm run verify` 与 Electron/desktop 单测都不代表真实 backend/浏览器验收；
  真实 backend 联调与主 line 完整验收前，一律 UNKNOWN；不允许宣称发布基线（integration 主验收未完成）。

## 12. 可转发实施线程 prompt

```text
你是 HyprialOS Tauri v2 桌面迁移实施线程。Worktree：
/Users/alicesmith/Sync/2-engineering/HyprialOS/worktrees/harness-bridge-tauri-v2（branch refactor/tauri-v2，
HEAD 必须仍为 1ea20ee0a964f2349903a4bab2e014345fa92733）。先读 docs/design/tauri-v2-migration.md 与
docs/evidence/tauri-v2-prep-2026-10-02/source-handoff.json，按 M0–M7 连续自主完成迁移→测试→修复。
普通 IR 发出后继续所有不依赖它的工作；只在不能绕过的接口漂移、新权限或外部依赖阻塞时停止。
不得直接把本树旧 Python 基线当作主线新结构发行物。

授权（写，仅限本树）：desktop/** 全部（含新建 desktop/src-tauri/**、cargo/tauri 初始化、依赖安装、
构建与测试），以及 docs/evidence/tauri-v2-prep-2026-10-02/impl/ 下你的证据。禁写：src/**、根 locks/CI/
.agents/skills、主迁移树与 /private/tmp integration（只读）；禁 git commit/push/reset/stash；禁覆盖
source-handoff.json 与既有证据；禁读用户凭据/生产 daemon/config；模型 API 认证是运行设施不得打印；
Python 一律 uv；临时文件 mktemp；loopback 测试用一次性 home。

硬性要求：src/gui 源码/锁逐字节只读（本树 ignored node_modules/dist 可由安装/构建命令生成）；
M0 保存 desktop 原字节 checkpoint；逐里程碑核对 GUI handoff；遵守 §3 冻结接口与 §10 安全反例；
测试替换按 §9 映射不许 skip；结构 ≤7 direct files/目录、≤1000 行/文件；共享 gate/CI 需求只发 IR 不私改；
win-x64 与真实 backend 未实测时标 UNKNOWN/BLOCKED；可以报告实际完成的桌面范围，
不得把它冒充整个主线重构完成或发布通过。不为补齐旧功能重写纯 Node 逻辑或扩充业务能力。

交付：按 M7 输出 desktop-only delta（pre/post sha256/mode/op）到 impl/desktop-delta.json，附验证命令
输出与边界/UNKNOWN 清单，然后停止等待主模型审阅。
```

---

本文件为规划交付。Tauri 迁移在实施线程完成 M0–M7 并由主线审阅采纳之前，状态为**未实施**。
