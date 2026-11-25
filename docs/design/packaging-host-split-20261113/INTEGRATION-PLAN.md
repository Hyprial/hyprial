# 并行工作流合并计划（2026-11-13）

本文件协调 W1（setup 页）、W2（C1 后端）、W3（`--target`）与 A 主线的合并顺序、
冲突点和验证门。主方案见 `docs/design/packaging-host-split-20261113.md`。

## 1. 分支/工作树

| 工作流 | 分支/工作树 | 基线 | 交付 |
|---|---|---|---|
| A 主线 | `build/packaging-host-20261113`（本工作树） | origin/dev `7d4dc766` | 边界门、宿主契约、集成 |
| W1 setup 页 | 子代理 isolated worktree | A 主线 HEAD | `clients/native/setup-ui/**` + native 接线 |
| W3 `--target` | 子代理 isolated worktree | A 主线 HEAD | `resolve-target.cjs` + 构建参数化 |
| W2 C1a | `feat/native-host-contract-20261113`（独立 dev worktree） | origin/dev `7d4dc766` | request_http launch/private + contract JSON |
| W2 C1b | 待建（从 C1a） | C1a | initialize/promotion/auth.logout |

## 2. 合并顺序

1. **C1a → dev 候选**：先审 request_http launch/private/contract；由 backend owner 复核。
   不直接合入 A；A 只消费契约。
2. **W1 → A**：setup 页与 native 接线；基于 A HEAD，合并时保留 A 的
   `core::init_script(false, false)`、边界门和 `host_contract` 校验。
3. **W3 → A**：`--target`；与 W1 可能同时改 `client/native/package.json`、
   `tauri-bundle.cjs`，按“setup 安装钩子 + target GUI 注入”两者都保留的顺序手工合并。
4. **A 集成**：跑边界门、Rust、Node、结构/模块门；用 `--target origin/dev` 做 dry-run。
5. **C1b**：在 C1a 之上补 initialize/post-reply/generation/auth.logout；A 侧 rebind shim
   到期条件写清。
6. **P6**：C1 进入目标分支后，`--target origin/pr-2727` 出包。

## 3. 冲突点

| 文件/区域 | 冲突 | 处理 |
|---|---|---|
| `clients/native/package.json` | W1 `build:setup` vs W3 可能新增 target 脚本 | 两者都保留；脚本名不重叠 |
| `packaging/clients/scripts/tauri-bundle.cjs` | W1 setup 安装 vs W3 prebuilt GUI 注入 | 先安装 setup，再安装 target `ui/gui`；两者目标目录不同 |
| `clients/native/src-tauri/src/windows.rs` | W1 setup 初始页 vs A 主页面 account 边界 | 保留 `init_script(false, false)` 与 `is_shell_surface` |
| `core/pages.rs` | W1 新增 setup 谓词 vs A `main_page()` 断言 | 不改 `main_page()`/`SHARED_PAGE` |
| `setup/mod.rs`/`product_consent.rs` | W1 sender fence vs A 边界 | sender fence 加 `is_setup_page`，不放宽 account |
| `upstream.rs` | A `host_contract` 校验 vs W1/W3 | W1/W3 不碰 upstream；C1a 提供契约块 |
| `src/hyprial/**` | C1 后端 | 只在 C1 工作树；A/W1/W3 不碰 |

## 4. 验证门

| 门 | 命令 | 适用 |
|---|---|---|
| A 边界 | `python scripts/ci/check_packaging_host_boundary.py --base origin/dev` | A/W1/W3 |
| Rust 宿主 | `cargo test --lib --features power-pack` | A/W1 |
| setup 单测 | `node --test clients/native/setup-ui/tests/*.test.mjs` | W1 |
| 打包脚本 | `node --test packaging/clients/tests/*.test.cjs`（按改动） | W3 |
| 结构/模块 | `check_structural_limits.py`、`check_module_boundaries.py` | A/W1/W3 |
| C1 后端 | `uv run --with pytest pytest <focused request_http/contract tests> -q` | C1a/C1b |
| target dry-run | `node packaging/clients/scripts/build-release.cjs --target origin/dev --dry-run ...` | W3/A |

## 5. 风险

- W1/W3 同时改 `tauri-bundle.cjs`：手工合并后必须跑 setup 安装 + target GUI 注入的
  两个测试，确保 `ui/setup` 与 `ui/gui` 不互相覆盖。
- C1a 只落 launch 契约；C1b 的 promotion/generation 是正式 2727 包的关键路径，不能把
  C1a 当作“已可出包”。
- W1 的 enter 信号若缺失，页面会停在 setup；这是正确的 fail-closed，不要用超时伪造 ready。
- 子代理提交只在本工作树；主线程复核后再合入，禁止直接 push/PR/merge。
