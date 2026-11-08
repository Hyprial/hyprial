---
name: gui-workflow
description: 历史记录：旧独立 src/gui、desktop 外壳及 Python GUI 安装/生命周期集成的构建与验证方式。2026-10-27 起该实现已移出运行路径，本 skill 不描述当前可用能力。
---

# 旧独立 GUI 工作方式（历史）

> **2026-10-27 退场：** 本文件记录的 `src/gui`、`app`、旧安装/生命周期入口已删除或改为
> fail-closed 占位。当前公开入口为裸 `hyprial gui` 与 `hyprial gui dev`，契约见
> `docs/guides/gui-dev-mode.md`；本 skill 不重复当前操作指引。以下命令与行为仅作为历史
> 实现记录，不代表本版可执行，也不是恢复旧兼容的要求。恢复范围、保留消费者与责任见
> `docs/design/gui-retirement-restore-ledger-2026-10-27.md`。

`src/gui` 是独立 ESM 工程 `@hyprial/gui`；桌面 `app` 是 Tauri v2 外壳，其主窗口加载
内嵌的 v11 Core 静态页（`src-tauri/ui/core`），经注入的五操作端口连接壳自己启动的
App Core host，**不再启动 `product/server.mjs` 这个 Node GUI 子进程**。`product/server.mjs`
仍是 `src/gui` 独立启动/CLI 产品路径的组合根，不是桌面窗口的实现。
DSH 只是与 codex/claude/pi 并列的外部 harness，其测试不是 GUI 验收。
下列命令从仓库根执行。事实源是实际 package scripts、配置实现与 CI；历史迁移证据
保留原文，不把旧插件实测或 D1 checkpoint 的结果当作新产品验收。

## 模块与权威边界

- `client/index.mjs`：composer、UI/Cordis 生命周期与工作区渲染。
- `session/index.mjs`：session port 与隔离 mock；真实 adapter 由 composition root 注入。
- `studio/index.mjs`：声明式 GUI 文档、revision、持久化与工具授权。
- `host/index.mjs`：可信 context 解析与注入端口组合；不从请求 body 取 principal/session 权限。
- `presentation/index.mjs`：共享样式和校验契约，避免 Client/Studio 双权威。
- `transport/index.mjs`：显式 driver 工厂；`product/index.mjs`/`product/server.mjs`：HTTP/server 组合根。
- `app/src-tauri/src/core/`：内嵌 Core 页、五操作注入端口与受控 App Core endpoint；
  默认 Core 构建与 power-pack 主窗口共用同一页面。
- `app/src-tauri/src/runtime/`：监督 daemon 与 App Core host 这两个 owned 子进程，
  boot 后把 endpoint 写入 Tauri managed state；不是在 Rust 重写 GUI 或业务权威。
- `app/src-tauri/src/{account,onboarding,windows.rs}`：shell 自有本地窗口与受限 IPC。
  主 GUI 窗口只由 `capabilities/power-pack/main.json` 授予 `app_core_call`，导航 fence 到
  该内嵌页；不在 Rust 重写 Cordis、会话、ACK、inbox 或 PAC。
- `app/src/update/`：唯一的 Node payload 更新实现；由随包固定 Node 执行，Rust 不复制其权威。

Tailcat 消费者使用 Python 提供的 v2 profile 与 `deviceReady` 状态；账户动作是
`status/login/device/forward`，不是旧 tsnet `join`。Rust 不生成默认 profile，不自行
定位/安装网络边车；这些权威留在 Python。`hyprial://join#v1.*` 仅作为 CLI 的 stdin
转发到 `org join - --json`，不进入 argv。排队、accepted-invite 轮询和 CLI 子进程
由桌面唯一 owner 取消并回收；测试只证明本机/fake 边界，不能宣称原生系统链接唤起
或真实组织授权已经通过。

跨 GUI 模块只消费各自 `index.mjs`，禁止另一模块 implementation import。
GUI/adapter 只能投影 daemon 持有的业务 session、inbox、turn 和 PAC 状态；不要从 UI、
聊天记录或 mock 推断业务完成，也不复制 daemon 的投递/恢复权威。
新增入站消费路径须遵守 daemon ACK 契约：只有真实消费/注入成功才 ACK，拒绝或失败不误 ACK；
会话切换不能混用旧 actor/绑定，可信身份和持久状态不能只存在于浏览器缓存。
当前新 GUI 未证明这些真实链路已完成，不能据此宣布完整功能等价。

Cordis 插件是受信任的本地代码，不是任意不可信插件的 sandbox。Studio 文档是受 schema
约束的布局/样式数据，不是 JavaScript、任意 CSS/资源 URL 或业务命令执行入口。
Agent 工具可准备自身设计会话的 draft/preview；发布、应用等用户操作的权限不能由参数伪造。

## 构建与早期门禁

使用 Node 24，依赖钉在 GUI 自身 lockfile：

```sh
npm --prefix src/gui ci --ignore-scripts
npm --prefix src/gui run check
npm --prefix src/gui test
npm --prefix src/gui run build
npm --prefix app ci --ignore-scripts
npm --prefix app test
npm --prefix app run build:core # static Core assets, requires GUI build:core
npm --prefix app run android:build # Android debug APK, including the Go mobile bridge
cargo fmt --manifest-path app/src-tauri/Cargo.toml -- --check
cargo clippy --locked --manifest-path app/src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --locked --manifest-path app/src-tauri/Cargo.toml
cargo clippy --locked --manifest-path app/src-tauri/Cargo.toml --all-targets --features power-pack -- -D warnings
cargo test --locked --manifest-path app/src-tauri/Cargo.toml --features power-pack
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
git diff --check
```

`check` 是 GUI 静态架构/public-export 检查，`test` 是 node:test，`build` 使用
`product/build.mjs` 生成 product assets。
不要手改生成物来绕过构建；修改来源后重建并审查产物差异。
Android debug APK 统一经 `npm --prefix app run android:build` 从 Go 源码构建并自动放置
`libmobilebridge.so`，再自检 APK 条目、ABI 与 C ABI 符号；禁止手工复制 jniLibs 或复用旧
`gen/android` 产物充当构建证据。
Rust 测试要求 GUI 依赖已安装且 PATH 有 Node ≥24。结构门覆盖 `src/hyprial`、`src/gui`、
`app/src`、`tests` 和存在时的 `app/src-tauri`（src/ui/examples/build.rs）：含代码目录最多 7 个直接文件
（同目录文档/数据也计数），每个代码文件最多 1000 行，测试 helper 与 Go fixture 不例外；依赖/生成物排除由门禁脚本定义，
不能靠改名或 per-file 豁免隐藏新代码。
活动 `scripts`、`contract`、`sidecar` 还执行同样的行数门；独立命令集合不按产品模块计直接文件数。
`sidecar` 中含 Go 源码的包目录不是独立命令集合，同用 7 个直接文件门，测试/文档/数据计入。
历史 `scripts/archived` 单独保留，不改写历史材料制造“全仓零超长”结论。
Python 跨域只消费 `kernel/identity/daemon/biz/shell` 的公共面；GUI 集成实现位于
`src/hyprial/shell/impl/gui`，根 `cli.py`/`gui_build_hook.py` 保持薄入口。

`.forgejo/workflows/gui.yml` 运行 GUI package 与 release 安全检查；
旧 desktop 验证/分发面（`desktop-check.yml`/`desktop-release.yml`）已随 app 树退役，不再存在。
仓库早期 CI 运行结构与 Python 公共边界门。先跑直接覆盖改动的测试，再按 AGENTS.md
跑 Python 全套与 Ruff；CI 绿、静态门绿和 mock 绿都不是 browser/E2E 结论。
深模块整线 topic PR 以精确的 `refactor/reconstruct-modules` 为 base；该分支向
`dev` 的 PR 同样运行全部组件门。不要用相似分支名或跳过 SHA/比较校验代替授权路由。

## 启动配置：缺失即 fail closed

独立 `src/gui` product（`npm --prefix src/gui start`）要求启动前显式设置：

- `HYPRIAL_GUI_TRANSPORT_DRIVER`：driver 名；未知或缺失报错，不默认选 mock。
- `HYPRIAL_GUI_PRINCIPAL_ID`：由可信 composition root/操作环境提供，不接受浏览器自报身份。
- `HYPRIAL_GUI_TRANSPORT_CONFIG`：可选 JSON object，内容由所选 driver 定义。
- `HYPRIAL_GUI_STUDIO_ROOT`：可选的持久化根；不设时使用 memory，不能声称跨重启持久化。
- `HYPRIAL_GUI_HOST`/`HYPRIAL_GUI_PORT`：默认 loopback/临时端口；对外暴露需要另行验证授权边界。
- `HYPRIAL_GUI_TOKEN`：可选预设 token（至少 16 字符）；不设置时 server 创建本次启动 token。

完成明确配置后，`npm --prefix src/gui start` 启动 `product/server.mjs`。
内置 `mock-fixtures` 仅供明确选择的隔离开发/测试，且其 config 必须指定 `fixturesDir`；
不把它作为真实 Hyprial backend 的 fallback。真实 driver 的连通、身份与 ACK 验收需独立证据。
桌面壳不读 `HYPRIAL_GUI_TRANSPORT_DRIVER`/`HYPRIAL_GUI_PRINCIPAL_ID`：它的可信身份边界是
固定 sender 的 App Core composition（`<app home>/app-core.json` 或绝对路径
`HYPRIAL_APP_CORE_CONFIG`），端点（AF_UNIX 或 Windows 本机 loopback TCP）由壳启动 host 后
只写进 Tauri managed state，页面拿不到 socket/端口/token。principal 不得来自浏览器自报。

Python detached launcher 使用 `HYPRIAL_GUI_LAUNCH_INFO_FILE` 的
`hyprial.gui-launch/v1` JSON receipt 和 HTTP readiness，设置 `HYPRIAL_GUI_NO_OPEN=1`；
该 receipt/URL 属于独立 GUI product 路径，桌面内嵌页不使用。URL/receipt 可能携带 token，
证据和日志必须脱敏。
历史 process record 只保留 ownership-checked 复用/退役。PID 相同不代表进程归属相同，身份 UNKNOWN 时不能另起或信号终止。

## 隔离验证与分发证据

测试 home、socket、端口、npm/uv cache 与日常环境隔离；不复制用户 auth 配置，不改生产
daemon/Lark/session/凭据。Python 命令全部经 uv；多 worktree 使用 task-owned
`UV_PROJECT_ENVIRONMENT`，避免互相替换 `.venv`。
运行真实 browser/backend/desktop 验证前，先确认使用专用测试 home 和测试身份；
若需要生产连接、凭据或重启活跃会话，停下并请求明确授权。

GUI 分发仅由产品 wheel 的 `hyprial/_gui/{release.json,gui.tar.gz}` 承担；
`shell/impl/gui/bundle.py` 与薄 `gui_build_hook.py` 核验精确来源、文件 hash/mode 和确定性 archive。
`hyprial gui start|status|stop|upgrade` 是内建命令，按 wheel 配对版本解包和监督 owned 进程；
`hyprial socialware install` 暂时返回 `COMING_SOON`，不再查询 catalog 或执行应用安装/挂载；旧一级 `hyprial install` 已退役（未知命令）。
捆绑树中的 `hyprial-install.json` 只为启动描述提供 `scripts/start-gui.sh`，
不能据此重新引入外部应用 installer。解包失败要保留旧源码和标记供恢复。
公开 snapshot 的 `src/hyprial/gui-source.json` 必须对应被选择的提交和输入；
未提交新源码是 release BLOCKED，不能重写旧 stamp、伪造 Git 历史或跳过 preflight。
desktop 打包需先检查对应平台 staged runtime 与已有 package scripts，不能把 unit test 当作
签名、安装或发布证明，也不因本 skill 获得 push/release 权限。
Node 与独立 GUI 仍是 payload 的 `entries.node` / `entries.gui`（更新工具与 CLI 产品路径使用），
但桌面窗口不再从 `entries.gui` 启动子进程；Core 页由 Tauri 资源内嵌。
release 使用 `app/scripts/build-release.cjs` 和 `probe-macos-dmg.py --release`（探针在
case 目录生成带标签的 smoke-only App Core composition，不再注入 GUI driver/principal）；
dev payload 不可作为 release 输入。外壳自更新尚未配置；Windows 验收须在 Windows 实跑。

交付记录每个命令和 PASS/FAIL/BLOCKED；未验证项写 UNKNOWN。
当前独立 GUI 的真实 backend、主产品浏览器交互、完整功能等价与发布切换仍须各自证据。
debug-only 桌面窗口自驱探针仅证明本地页面的 DOM 合成事件与隔离假后端交互，
不覆盖真实系统输入、原生菜单/对话框或真实账户与网络；各平台仍需分别执行。
不得用 mock/SSR 或打包 fixture 冒充通过。改变 agent 操作方式时说明 Skill-Impact；
字段/错误语义变化同步 contract 或给出 Contract-Impact 放行理由。

Rust 默认 feature 为空：Core 仅展示嵌入的静态聊天页，不启动本机 runtime。
`npm --prefix app run dev:core` 先构建该页再启动壳。
`--features power-pack` 保留 legacy Node GUI、进程监督及更新路径；发行脚本显式启用它。
这是分离旧 runtime 的过渡实现；共享 Core UI 上的本机管理已接入第一切片：`core-management`
窗口沿用只读列表，并在严格 port 形态（operations 与成员一一对应）下提供 agent `创建`
（`hyprial agent create --name …`）与 `启动`（`hyprial agent start <harness> --name … --headless`，
harness 限 claude/codex/pi/dsh），经 runtime 自有 CliIo 管道执行、按具名 code/message 如实反馈。
destroy、真实已安装 runtime 与真实模型的端到端验收仍待后续授权运行（unit/fixture 成功不算该证据）。
