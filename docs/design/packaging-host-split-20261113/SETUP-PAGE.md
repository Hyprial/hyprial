# 独立 setup 页面（P2 设计附录，2026-11-13）

本文件记录把安装检测/产品同意从业务 GUI 迁到 packaging-owned setup 页面的分析。状态：
**设计稿，待实现**。主方案见 `docs/design/packaging-host-split-20261113.md`。

## 0. 可执行契约

| 项 | 约定 |
|---|---|
| 源码根 | `clients/native/setup-ui/`，随 A 分支版本化 |
| 构建入口 | `npm --prefix clients/native run build:setup` → `node ../packaging/clients/scripts/install-setup-ui.cjs` |
| 运行时依赖 | 零 npm 依赖；只用浏览器 API + `__TAURI_INTERNALS__.invoke` |
| 禁止 import | `gui/**`、`src/hyprial`、`react`、`vite`、`@hyprial/gui-app` |
| 版本来源 | 宿主 `src-tauri/tauri.conf.json` 的 `version` + host SHA，由 install 脚本写入 `ui/setup/version.json`；页面只读不改 |
| 资源嵌入 | 复制到 `src-tauri/ui/setup/`；`tauri.conf.json` `frontendDist: "ui"` 自动嵌入；`ui/setup` 与 `ui/gui` 分离 |
| CSP | `script-src 'self'`；外部脚本/样式禁用；`verifyBundlePage(target,'index.html','setup UI')` |
| 目标 GUI 隔离 | `--target` 只替换 `ui/gui/`，install-setup-ui 只写 `ui/setup/`，两者不互相覆盖 |
| 测试 | `node --test clients/native/setup-ui/tests/*.test.mjs`；CI 边界门扫描禁用 import/依赖 |
| provenance | 记录 `setupUiTree` digest、host commit/tree、版本文件 digest |

## 1. 最小源码集

`clients/native/setup-ui/`（plain ES modules，避免引入 Vite/React/Tailwind 第二套工具链）：

| 文件 | 作用 |
|---|---|
| `index.html` | 页面骨架、CSP、引用 `setup.js`/`setup.css` |
| `setup.css` | 现有 `startup-wizard.css` 的 token 子集 + 纯按钮样式（不依赖 shadcn/Tailwind） |
| `setup.js` | 状态机：读取 product state、同意/重试、harness 行、进入信号 |
| `product-bootstrap.js` | 从 `gui/src/platform/product-bootstrap/{contract,client}.ts` 移植的 envelope 解析与 Tauri invoke（`product_prepare_state`/`product_prepare_consent`） |
| `harness.js` | `window.__hyprialSetup.harnesses()` 调用与行投影（不引入 `platform/setup` 的 transport） |
| `assets/hyprial-mark.svg` | 品牌 mark |
| `tests/*.test.mjs` | 纯解析/状态机测试（不依赖浏览器） |

不迁移：`StartupWizard.tsx` 的 React 结构/`FirstRunActions`/`startup-connection`/
`ProductBootstrapGate`/native account capabilities。

## 2. 状态机

1. 加载：`invoke('product_prepare_state')`；校验 `contract`/`revision`/`busy`/`state`；
   malformed/缺失按错误处理，绝不当作 ready。
2. `needs-consent`：展示同意卡片；点击后调用一次 `product_prepare_consent`（长调用，
   显示“准备中”）；返回后重新读取 state。`blocked`/`initializing`/失败 → 显示原因/重试，
   重试先重读 state，不重复写。
3. `ready`/`not-applicable`：展示 Python 版本/来源；调用 `__hyprialSetup.harnesses()`，
   `SETUP_UNAVAILABLE` 重试（20×500ms）；Pi/Claude Code/Codex 仅展示，不阻塞。
4. 进入：调用新的 `setup_invoke {action:'enter'}`（或专用 `product_prepare_enter`），
   页面只显示“正在启动”，导航交给 native。**不调用 `/daemon/session`**，不自己起后端。

## 3. Native 接线

- `core/pages.rs`：新增 `SETUP_PAGE="setup/index.html"`、`is_setup_page`、
  `is_shell_surface(url, origin)`（setup|main|host）。不改 `main_page()`/`SHARED_PAGE`
  （app-core relay、probe、product driver 都断言它）。
- `windows.rs`：主窗口初始 `WebviewUrl::App(SETUP_PAGE)`；导航守卫改用 `is_shell_surface`。
  `core::init_script(false, false)` 已注入 `__hyprialSetup`；`capabilities/power-pack/main.json`
  已有权限。
- Sender fence：`power_pack/product_consent.rs` 与 `setup/mod.rs` 的 `sender_allowed` /
  `setup_sender_allowed` 增加 `is_setup_page`；否则 `product_prepare_*`/`setup_invoke`
  会返回 “Invalid ... sender”。
- Enter 信号：app setup 里放 `SetupEnter { entered: AtomicBool, notify: Notify }`；命令校验
  sender 后置位；`start_after_login` 在 `bootstrap.wait_boot_ready()` 后等待它；只在本进程
  启动时重置。若页面永不发信号：保持 setup 页 + 诊断，不伪造 ready。
- 启动顺序保持现有：enter → startup 进度窗口 → `boot`/`boot_bootstrap` → `start_gui_host`
  → `main.navigate({origin}/gui/index.html#grant=…)` → `show_main` → promotion watcher。

## 4. 构建/安装

- 新 `packaging/clients/scripts/install-setup-ui.cjs`：复制 `clients/native/setup-ui` 到
  `clients/native/src-tauri/ui/setup/`，用 `verifyBundlePage(target,'index.html','setup UI')`
  校验外部脚本/样式（CSP `script-src 'self'`）。
- `tauri-bundle.cjs` 在 `prepareSharedUi()` 旁调用它（覆盖 `build-release.cjs` 与
  `bundle-dev.cjs`）；`clients/native/package.json` 增加 `build:setup` 并串入 `dev`。
- `src-tauri/ui/setup/` 加入 `.gitignore`（与 `ui/gui/` 一致）；`tauri.conf.json`
  `frontendDist: "ui"` 自动嵌入。
- setup 页与 P4 的目标 GUI 注入解耦：目标 `gui/dist` 只替换 `ui/gui/`，绝不覆盖 `ui/setup/`。

## 5. 风险

| 风险 | 缓解 |
|---|---|
| Sender/nav fence 遗漏 | 三处 allowlist + 精确 source 测试；缺一处会 “Invalid sender” 或首屏被拒 |
| 复用 `main_page()` 破坏 app-core/product driver | 新增独立 setup 谓词，不改 main_page |
| 无 enter 门 | 增加信号；页面不发信号时保持 setup + 诊断，不伪造 ready |
| 预运行时 harness 检测无 runtime paths | 用 `read_only_cli_io` 或保留重试；否则行显示 unavailable |
| product-driver Mode::Local 期望 `/gui/index.html` | 该模式仍走 main page；setup 仅用于正常启动 |
| 与业务 GUI 的 StartupWizard 重复/漂移 | B 移除 seam 前保持契约字符串同步；setup 页是唯一 A 侧实现 |
| 样式/字体依赖 | 只复制 token 子集，不引入 fontsource/Tailwind |

## 6. 验收

- fresh home：setup 页可完成同意/安装；harness 行显示；点击进入后 native 启动并导航。
- 已有环境：直接显示 ready，不重复同意。
- 拒绝/取消/失败：保持 setup 页，可重试；无伪造 ready。
- sender fence：非 setup/main/host 页面调用被拒。
- 构建：`install-setup-ui` 校验通过；`ui/setup` 进入 Tauri bundle；P4 目标 GUI 不覆盖它。
