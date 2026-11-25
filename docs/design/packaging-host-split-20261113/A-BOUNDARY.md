# A 层边界与门禁（2026-11-13）

本文件把三层拆分中的 A 层从“文字约定”变成**可执行边界**。它回应 Codex review 的
第 1、5 条；机器可读清单见 `A-BOUNDARY.json`，CI 入口是
`scripts/ci/check_packaging_host_boundary.py`。主方案见
`docs/design/packaging-host-split-20261113.md`。

## 1. 结论

A 分支只承载**打包宿主**：native 窗口/boot/GUI host/setup 页、打包脚本、宿主 CI。
A 不带业务 GUI，不带 `src/hyprial` 业务或后端契约，不向业务页面注入旧 native account
bridge。

旧 native account/onboarding 代码作为**遗留冻结层**保留在 A 的源码里，但：

- 默认构建不注入 `window.__hyprialAccount`；
- 主窗口 capability 不授予 `allow-account-invoke`；
- `network-worker.py`、旧 account/onboarding 窗口只作为遗留路径，不被新 setup 页或 B 消费；
- 后续删除单独排期，不作为新路线依赖。

## 2. 归属清单

| 类别 | 内容 | 处置 |
|---|---|---|
| A 所有 | `clients/native/**`（窗口、boot、GUI host、setup 页、打包宿主桥）、`packaging/clients/**`、`scripts/ci/**` 中宿主门、宿主测试 | 随 A 分支演进 |
| A 遗留（冻结） | `clients/native/src-tauri/src/account/**`、`onboarding/**`、`platform_account/**`、`runtime/product/**` 中的 `network-worker.py` 路径、`capabilities/power-pack/account.json`、`onboarding.json` | 保留源码；默认不注入主页面；不进入 B 的 capability |
| 必须迁出 A | `gui/src/features/product-bootstrap/**`、`gui/src/app/native-capabilities.ts`、`gui/src/platform/product-bootstrap/**`、对应 `gui/tests/**` | 归 B（2727 权威）；A 只保留行为参考，不复制 React 组件 |
| A 新实现 | `clients/native/setup-ui/**`（plain ES modules） | 行为参考 StartupWizard，但不 import `gui/**` |
| 禁止进入 A | `gui/**`、`src/hyprial/**`、`contract/**`（C 的契约载体）、`tests/daemon/**`、`tests/shell/onboarding/**` | 变更应落在 B/C 分支 |
| 目标输入 | 目标分支的 `gui/dist`、`src/hyprial` wheel、`uv.lock` | 只在打包时经 staging 注入，提交进 A 即失败 |

## 3. 旧 native account bridge 的精确处置

当前事实（2026-11-13）：

- `clients/native/src-tauri/src/windows.rs` 的 `main_window` 曾调用 `core::init_script(false)`；
- `core/port.rs` 在 desktop + `power-pack` 下无条件返回 `ACCOUNT_BRIDGE_INIT`；
- `capabilities/power-pack/main.json` 含 `allow-account-invoke`。

这与主方案 §3.2“不注入”冲突。本批改为：

1. `init_script(mobile, account_bridge)`：主窗口传 `false`，不再注入
   `window.__hyprialAccount`；
2. `main.json` 移除 `allow-account-invoke`；
3. 旧 account/onboarding 窗口若仍保留，只使用各自的 capability，不进入主页面；
4. smoke 探针改为验证主页面**没有** `__hyprialAccount`，而不是要求它存在；
5. 后续删除遗留模块时，再移除 `account_invoke`/`onboarding_invoke` 注册。

## 4. setup 页边界

`clients/native/setup-ui/**` 必须满足：

- 纯 ES modules，无 Vite/React/Tailwind 构建；
- 零 runtime npm 依赖；
- 不 import `gui/**`、`src/hyprial`、`react`、`vite`、`@hyprial/gui-app`；
- 只通过 `__TAURI_INTERNALS__.invoke` 调用 `product_prepare_*`、`setup_invoke`；
- 构建入口 `npm --prefix clients/native run build:setup` → `install-setup-ui.cjs`；
- 复制到 `src-tauri/ui/setup/`，由 `tauri.conf.json` 的 `frontendDist: "ui"` 嵌入；
- 版本/hash 来自宿主 `tauri.conf.json` + host SHA，随 provenance 记录；
- 目标 GUI 只替换 `ui/gui/`，绝不覆盖 `ui/setup/`。

## 5. 门禁

`scripts/ci/check_packaging_host_boundary.py` 在 A 分支的 PR 上检查：

- 相对 dev merge-base 的 changed paths 必须在 A 允许清单内；
- `gui/**`、`src/hyprial/**`、`contract/**` 改动直接失败；
- `clients/native/src-tauri/ui/gui/**` 不得被 git 跟踪；
- `capabilities/power-pack/main.json` 不得含 `allow-account-invoke`；
- `windows.rs` 的 `main_window` 必须使用 `core::init_script(false, false)`；
- `setup-ui/**` 不得出现禁用 import/依赖；
- `OWNERSHIP.tsv` 中 owner=B/C 的路径不得出现在 A 分支 diff；
- 非 packaging-host 分支返回 `not-applicable`，不误伤其他 PR。

门禁脚本和测试属于 A；契约 JSON 属于 C，A 只读取/预检。

## 6. 验收

- `cargo test --lib --features power-pack` 通过（含新的 init_script 边界测试）；
- `uv run --no-project --python 3.12 pytest tests/tooling/packaging_host/test_check_packaging_host_boundary.py`
  通过；
- 对当前 A 分支跑门禁为 PASS；
- 人为在测试 fixture 里加入 `gui/**` 或 `src/hyprial/**` 时门禁失败；
- 主窗口 smoke 不再依赖 `__hyprialAccount`。
