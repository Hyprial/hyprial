# Rust Core / PowerPack 分层切片（2026-10-08）

状态：第一个可评审切片。桌面壳（`app/src-tauri`）按一个 Cargo feature 分成两层；
Cargo 层的证据见下文“验证”，其中**没有**任何真实窗口、真实 backend 或移动端结论。

## 1. 动机与范围

现在只有一个 Tauri 壳：`default` 构建里就包含 payload 选择、startup phase、账号/引导窗口、
owned 子进程监督与更新面。切片目标是把“只要一个静态本地页面的壳”和“完整 legacy 壳”
分成两个构建层，同时**不改**已发布身份、载荷语义与发布路径：

- `default = []` → **Core**：打开静态本地 `core/index.html` 的独立壳。
- `--features power-pack` → **PowerPack**：现有完整 legacy 壳（原样搬移）。

这是隔离 legacy runtime 的过渡切片，**不是**共享 Core UI + 可选本机管理 UI 的最终 PowerPack；
最终共享界面及本机管理端口仍待接入。

本切片不做：Core 的连接端口/命令（后续）、GUI 侧静态 Core 产物（依赖 PR #1320）、
任何真实聊天/浏览器/原生窗口验收。

## 2. 行为契约

### Core（`default = []`）

- 单独窗口（label `core`）加载打包进去的静态页面 `ui/core/index.html`（`WebviewUrl::App`），
  创建即可见；`ui/core` 由 `npm run build:core` 从 GUI 的 `dist/core` 整体拷贝而来。
- 不读 runtime manifest、不解析 payload 根、不 spawn Python/Node/daemon、不进入 startup
  phase/onboarding/update 路径：这些模块在 Core 里**根本不参与编译**（`src/lib.rs` 的
  `#[cfg(feature = "power-pack")]`），不是运行期判断。
- 窗口无任何原生命令：`build.rs` 在 Core 下不声明 app command，发现的唯一 capability
  （`capabilities/core/core.json`）权限列表为空，且只作用于 `core` 窗口。导航围栏只允许
  `core/index.html`（`tauri://localhost` / `http://tauri.localhost`），新窗口与下载均拒绝。
- 若打包产物里没有该页面，Core **fail closed**：启动失败并打印缺页提示，不打开空白窗口。
- `app > security > capabilities` 不再出现在 `tauri.conf.json`（见 §3.2）。

### PowerPack（`--features power-pack`）

- `src/power_pack/mod.rs` = 原 `src/app.rs` 的 run 实现原样搬移（phase 序列、更新机制、
  manifest/storage 语义、`main` 窗口与本地页面都不变）。
- 旧模块（account/onboarding/deeplink/menu/runtime/shell/startup/supervisor/update/windows）
  仍编译，但只在 `power-pack` 下；旧 capability 文件原样搬到 `capabilities/power-pack/`
  （identifier、window 作用域、permission 全部不变）。
- `tauri-plugin-single-instance` 依赖被限制为 desktop 目标
  （`cfg(not(any(target_os = "android", target_os = "ios")))`），其它 legacy 插件依赖全部
  `optional`，只由 `power-pack` 打开。
- `power-pack` + mobile：`src/lib.rs` 直接 `compile_error!` 拒绝（该层监督 owned 子进程、
  用 desktop-only 插件），不静默退化成 Core。

### 保持不变（R9 身份冻结）

`package.json` 的 `productName`/`name`、`Cargo.toml` 的 `hyprial-desktop`/`hyprial_desktop`、
`tauri.conf.json` 的 `productName`/`mainBinaryName`/`identifier`、以及 legacy userdata
（`~/.hyprial-desktop-prototype`、`%APPDATA%\Hyprial Desktop Prototype`、smoke/probe 临时目录）
全部不动；legacy 启动参数、phase 名、更新通道语义不动。

## 3. 分层落点：一个 feature，四处受它约束

### 3.1 Cargo feature（`app/src-tauri/Cargo.toml`）

`default = []`；新增 `power-pack` feature 用 `dep:` 打开**每一个** optional 依赖
（serde/tokio/url/uuid/regex/sha2 + 5 个插件 + libc/windows-sys）。
`cargo tree --offline --depth 1`：Core 直接依赖为 `tauri` 和宏展开必需的 `serde_json`；`power-pack` 才是完整集合。
`[lib] crate-type = ["staticlib", "cdylib", "rlib"]`（移动端需要的目标类型），
`run()` 带 `#[cfg_attr(mobile, tauri::mobile_entry_point)]`。

### 3.2 命令与 capability 由同一个分支决定（`build.rs`）

```
default      → commands(&[])                            + capabilities/core/**/*
power-pack   → commands(&["account_invoke","onboarding_invoke"]) + capabilities/power-pack/**/*
```

- 二者同源于 `cfg!(feature = "power-pack")`，所以“生成的 permission”与“被选中的 capability”
  不可能错位；权限名不匹配时 tauri-build 的 `validate_capabilities` 直接报错。
- **不用** `app > security > capabilities` 白名单：按钉住的 tauri-utils 2.10.1
  `get_capabilities`，空数组的含义是“磁盘上发现的**全部** capability”，而显式列表会是第二处
  真相（且 Core 构建里必须点名它并不生成的 legacy permission）。因此改为“feature 选目录”，
  配置里不再出现该键——与仓库“R1 单一寻址 / 不维护代码侧白名单”的取向一致。
- 自定义 discovery pattern 会取代 tauri-build 默认的 `./capabilities` 监视，所以 build.rs
  自己 `rerun-if-changed` 两个目录（`capabilities/core`、`capabilities/power-pack`）。

### 3.3 入口分派（`src/lib.rs`）

`pub mod core; pub mod error;` 共享；11 个 legacy 模块逐个 `#[cfg(feature = "power-pack")]`；
`run()` 在两个 feature 分支各返回对应入口的退出码，`tauri::generate_context!()` 由真正运行的
那一层调用。`error.rs` 里属于载荷平面的 `node_platform`/`node_arch` 也随 feature 收起。

### 3.4 发布路径（`app/scripts/tauri-bundle.cjs`）

抽出的纯函数 `tauriBuildArgs()` 固定追加 `--features power-pack`（`build-release.cjs` 与
`bundle-dev.cjs` 都经它出包），防止“出包时静默变成 Core 壳”。`--features` 支持逗号分隔已用
钉住的 CLI 2.12.1 `tauri build --help` 实测确认。

### 3.5 静态 Core 页面（`app/scripts/build-core-ui.cjs`）

`npm --prefix app run build:core`：Node≥24 校验 → 调 `npm --prefix src/gui run build:core`
（GUI 自己的静态构建；缺失时**显式报错退出 1**，不 skip）→ 整目录拷贝到
`app/src-tauri/ui/core`（先删旧输出）→ 校验 `index.html` 存在、非空、每个 `src`/`href`
都是相对路径、不越出 bundle、目标文件确实存在。
`npm --prefix app run dev:core` = 同样的准备 + `tauri dev`（默认 feature，即 Core），
不 staging 任何 Python/Node payload。`ui/core` 是生成物：写进 `app/.gitignore`，
仓库里不放 mock 占位页；生成目录遵循原有每层最多 7 个源码文件约束，不新增结构门豁免。

## 4. 命令与实测结果

| 命令 | 结果 |
| --- | --- |
| `cargo fmt --manifest-path app/src-tauri/Cargo.toml -- --check` | PASS |
| `cargo tree --offline --depth 1`（默认） | PASS：直接依赖为 `tauri`、`serde_json`（dev: tempfile） |
| `cargo tree --offline --depth 1 --features power-pack` | PASS：完整 legacy 集合 |
| `cargo check --offline`（本机 Linux） | **BLOCKED**：缺 `pkg-config`/libdbus/webkit2gtk 等系统库（按纪律不安装）；未取得编译结论 |
| 隔离 build-script 探针（`/tmp/hyprial-buildrs-probe-20261008`，只用钉住的 tauri-build 2.7.1） | PASS：见 §5 |
| `node --test app/tests/feature-layers.test.cjs app/tests/core-ui-build.test.cjs` | PASS（13/13） |
| `npm test --prefix app` | PASS（130 pass / 0 fail / 2 skip；skip 为既有用例） |
| `cd app/src-tauri && cargo test`（Core 与 `--features power-pack`） | **BLOCKED**（同上，无系统库；由 parent 在 Windows 上执行） |
| GUI 静态资产 | 来自 PR #1320 的真实 `dist/core`（root 4 文件 + runtime/5 模块），已复制用于 Windows 编译；本独立分支依赖该 PR，CI 不允许缺资产时 skip |
| 真实窗口打开、真实 backend、移动端 | **UNKNOWN**：本机无 GTK/显示、无 Android/iOS SDK，未实测 |

## 5. build-script 隔离探针（仅构建脚本行为证据）

临时 crate（`/tmp`，不提交）复制真实 `build.rs` + `capabilities/` + `tauri.conf.json`，
只依赖钉住的 `tauri-build = "=2.7.1"`，用 `DEP_TAURI_DEV=false` 补偿探针没有 `tauri` 依赖：

- 默认分支 → `OUT_DIR/capabilities.json` 恰好是 `{core}`，`permissions: []`，
  `windows: ["core"]`；没有 legacy permission 校验错误。
- `--features power-pack` → 恰好 `{account, onboarding}`，各自带
  `allow-account-invoke` / `allow-onboarding-invoke`，与 build.rs 声明的命令一一对应，
  tauri-build 未报 “Permission ... not found”。

它证明的是 build.rs 的 feature→(命令, capability) 映射与钉住 tauri-build 的实际行为一致；
**不**替代整机 `cargo check`/`cargo test`，也不证明窗口行为。

## 6. 测试（`app/tests/`，node:test）

- `feature-layers.test.cjs`：解析 Cargo.toml 断言 `default` 为空、非 optional 依赖只有
  `tauri` 与 `serde_json`、每个 optional 依赖都被 `power-pack` 打开、lib crate-type；解析 build.rs 两个分支，
  断言“每层发现的 capability 权限集合 == 该层生成的命令”，Core 层命令为空且唯一 capability
  权限为空、且其 `windows` 与 `src/core/mod.rs` 真正打开的窗口 label 一致；断言
  `tauri.conf.json` 不写 capability 列表、不放开远端、身份字段（R9）与包名/hash 不变、
  旧的 `runtime_probe` example 需要 `power-pack`、Core 源码不引用任何 legacy 模块或进程 API。
- `core-ui-build.test.cjs`：真调 `tauriBuildArgs()` 断言出包 argv 必带 `--features power-pack`；
  用临时 fixture 树真跑 `installCoreUi()`（整树拷贝、清掉旧输出、拒绝绝对/外部/越界/缺失引用、
  空页面、缺 index），用注入的假 spawn 断言 GUI 构建的调用形式与“缺 build:core 即显式阻塞”，
  并断言 `build:core`/`dev:core` 命令文本不依赖 payload staging。
- `tauri-shell.test.cjs`（更新）：legacy capability 断言改读 `capabilities/power-pack/`，
  command 声明/handler 断言改读 build.rs 的 power-pack 分支与新位置 `src/power_pack/mod.rs`，
  并把 capability 选择语义写成显式断言。

## 7. 明确未完成 / UNKNOWN / BLOCKED

- **UNKNOWN**：Core 窗口的真实打开与渲染；移动端编译（无 SDK；本切片仅配置 crate-type、
  mobile entry point 与 `power-pack + mobile` 的 `compile_error!`）。
- 本独立 Rust 分支依赖 PR #1320 的 `build:core`。组合分支需先生成并拷贝资产再跑 Cargo；CI 已显式执行该步骤，缺依赖时失败。
- Core 的 frontendDist 仍是同一个 `ui/`：Core 构建里也嵌入 legacy 本地页面，但它们既无窗口
  也无 capability，主框架导航守卫拒绝切换至这些页面；把 dist 按层拆开是后续项。CSP 目前两份构建共用（含
  `connect-src ipc:`），Core 收紧 `connect-src` 需要按 feature 分化配置，未在本切片做。
- Core 尚未提供任何 invoke 命令与连接端口（后续切片），因此没有任何真实 backend 交互证据；
  本文件不声称真实聊天、原生窗口或浏览器验收通过。
- 未在 Windows/macOS 出包；未跑 Python 全套（本切片只改壳层与脚本，无 Python 面变化）。

## 8. 原生 Windows 审修记录

使用任务专属 Windows scratch 与 Cargo cache；无系统工具安装、无应用窗口启动。
组合 Rust 本切片 + Windows 修复 `90047d051` + PR #1320 的静态 Core 资产。
首次真实默认编译发现 `generate_context!` 需要直接 `serde_json` 依赖；已修正默认层依赖，
这说明 feature/source 扫描与 build-script 探针不能替代完整编译。
修复后 `cargo clippy --locked --offline --all-targets -- -D warnings` PASS（5.42 秒）。
默认层检查包含 binary/lib/examples/test targets；尚不代表原生窗口或真实聊天。

`cargo test --locked --offline --lib`（默认 Core）PASS：2 passed / 0 failed / 0 ignored；
实际编译链接 2m02s，运行 0.00s。覆盖本地页面主导航白名单（路径、查询、用户信息、端口），
不启动窗口。钉住 Tauri 2.12.1 的 `run_return` 可用于 mobile（文档注明 iOS 回退 run），
mobile 宏的 stop_unwind 接受泛型返回类型；此源码核对不替代 Android/iOS SDK 构建。

`cargo clippy --locked --offline --all-targets --features power-pack -- -D warnings` PASS（12.65s）。
`cargo test --locked --offline --features power-pack --lib runtime::daemon::tests::` PASS：
7 passed / 0 failed / 0 ignored / 113 filtered out（构建24.17s，运行0.01s）。
验证端点文件解析、私有 named-pipe 命名空间、4096 字节上限和未支持方法提前拒绝，
未验证真实 named-pipe server 往返。Windows 完整 legacy 测试集未执行：本机 Node 22，
GUI/runtime 测试要求 Node 24；不将平台排除或环境缺失记作通过。
本分支 Node 壳测试130 pass / 2既有skip；fmt、diff与结构门通过。

实现来源：真实 Claude Code worker（deepseek-flash）在独立 worktree 实施，协调者独立审修
导航边界、Windows npm launcher、默认依赖和结构门，并执行上述原生验证。
