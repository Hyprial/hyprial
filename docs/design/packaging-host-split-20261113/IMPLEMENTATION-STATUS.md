# 打包宿主拆分实施状态

本文件是 `build/packaging-host-20261113` 的活状态记录。时间使用本地时间（WSL）。
阶段汇报经本地 Hyprial squire 路由发送到 Feishu；详细证据以仓库提交和测试输出为准。

## 1. 基线

- 分支：`build/packaging-host-20261113`
- 基线：dev `7d4dc7668e6b95dd580f9dbd1eeb50c16469515e`
- 已完成：P0 初版 OWNERSHIP、P1 A 层 native/packaging 移植、P2/P3/P4 设计附录
- 当前阶段：Codex review 后补边界、契约版本和 `--target` 执行细节

## 2. Codex review 处置

| # | 问题 | 判定 | 处置 | 状态 |
|---|---|---|---|---|
| 1 | A 边界不干净，account bridge/onboarding 仍归 A | 成立，代码实际注入 `__hyprialAccount` | A-BOUNDARY + 主页面停止注入 + capability 移除 | 完成（代码+门禁） |
| 2 | setup-ui 技术形态未定死 | 部分成立：附录已定 plain ES modules，主文档/OWNERSHIP 冲突 | 统一口径 + setup-ui 契约 + no-gui 门 | 设计完成，待 P2 实现 |
| 3 | `--target` 接口不可执行 | 成立（执行层） | 补解析/清理/失败语义 + `resolve-target.cjs` | 设计完成，待 P4 实现 |
| 4 | 宿主契约没有版本化载体 | 部分成立：有 HTTP 版本，缺生命周期版本 | `NATIVE-HOST-CONTRACT.md` + C1 JSON/预检 | 设计完成，待 C1 |
| 5 | P0 只有人工分类，没有结构门 | 成立 | `check_packaging_host_boundary.py` + CI | 完成 |

## 3. 工作流

| 工作流 | 内容 | 负责人 | 状态 |
|---|---|---|---|
| W0 边界与门禁 | A-BOUNDARY、account 注入修复、CI 门、smoke 调整 | 主线程 | 完成（`fc2a99e83`） |
| W1 setup 页 | `clients/native/setup-ui` + native 接线 + 测试 | 子代理（isolated worktree） | **完成并合入 A**：`da640e32a`；29 setup 单测 + 7 install 测试；Rust 526 passed |
| W2 C1 后端契约 | C1a launch/private + contract JSON；C1b initialize/promotion | 子代理（独立 dev worktree） | **C1a+C1b 完成并推送**：`431012be3` + `d8fed9925`；253 focused tests；待 backend owner |
| W3 `--target` | resolve/clean/preflight/provenance/cleanup | 子代理（isolated worktree） | **完成并合入 A**：`736e5d260` + `4b1e00d6c`；targeted 85/86 passed（1 skip）；runtime contract + `uv lock --check` 已接 |
| W4 集成/出包 | C1 + setup + target 合并，Mac/Windows 验证 | 主线程 | 未开始 |
| W5 C2a+C2b | 平台 PTY/ACL + harness CLI（从 15c0b3707 移植） | 子代理（`feat/c2-platform-harness-20261113`） | **完成并推送**：`23e469f63`；219 passed / 1 skipped |
| W6 真 target preflight | 隔离 daemon/fake backend + contract 预检 | 子代理（`feat/preflight-daemon-20261113`） | **完成并合入 A**：`48ec0d6be`；39 tests + **真实 C1a wheel e2e PASS** |
| W7 2727 集成 | `intg/2727-c1-20261113`：PR 2727 + C1a/C1b/C2 | 子代理（isolated worktree） | **完成**：`97e025a80`（base `3b4e654a3`）；280 + 219 passed；已 push（本地集成分支，无 PR） |

## 4. 进度日志

> 时钟更正（2026-11-13 14:15 CST）：此前条目的小时数比实际 WSL 本地时间快约 4 小时
> （提交时间戳可交叉核对，如 Linux 完整包提交为 14:10 +0800）。以下新条目以 `date`
> 实际时间为准；旧条目不追改，避免伪造历史。

### 2026-11-13 11:59 CST

- 完成 Codex review 评估；确认 #1 不是文档问题：`windows.rs:130` 主窗口注入
  `__hyprialAccount`，`main.json` 授予 `allow-account-invoke`。
- 新增 `A-BOUNDARY.md`、`A-BOUNDARY.json`、`NATIVE-HOST-CONTRACT.md`。
- 改 `core::init_script(mobile, account_bridge)`：主窗口传 `false`，setup bridge 仍注入，
  legacy account helpers/assignment 用占位符按需插入；`main.json` 移除
  `allow-account-invoke`。
- smoke 探针改为验证 setup bridge + 主页面无 `__hyprialAccount`；host_bridge ACL 测试改为
  断言主页面没有 `account_invoke`。
- 新增 `scripts/ci/check_packaging_host_boundary.py`、7 个单测、gate-python CI step。
- 修复 upstream readiness fake sidecar 的 `launchNonce` fixture（原测试依赖缺失 nonce）。
- A 侧新增 `host_contract` 解析/拒绝路径（server.json + catalog）：校验
  `hostContractVersion`、launch 三能力、lifecycle 字段、requiredPhases/requiredMethods；
  缺少整块时按 legacy 接受，C1 落地后再收紧。
- A 侧新增 `AuthorityFile.transportGeneration` 与 `Upstream::rebind()`：重读原子重发布的
  `server.json`，generation 变化时替换 addr/token；空 generation 拒绝。为 C1b 的
  external promotion 做准备。
- 提交 `fc2a99e83`；committed HEAD 的边界门 PASS（86 changed paths，0 findings）。
- 阶段汇报已发 Feishu 负责人私聊路由（路由名已脱敏），messageId
  `ac7a40af-a910-5745-a386-e9068cd35633`，accepted=true（2026-11-13 12:06）。
- 并行启动三个子代理：W1 setup 页（isolated worktree）、W3 `--target`（isolated
  worktree）、C1a 后端 launch 契约（`feat/native-host-contract-20261113`，从 dev）。
- 下一步：接收子代理结果、逐项复核并合并；C1b（initialize/promotion/auth.logout）
  在 C1a 后接续。

### 2026-11-13 12:40 CST

- A 侧补 `transportGeneration` 解析与 `Upstream::rebind()`，为 C1b external promotion
  做准备；Rust 519 passed（仍仅 1 个既有 Windows 路径失败）。
- 子代理进度（工作树观察，未提交）：W1 已创建 `clients/native/setup-ui/`；W3 已改
  `build-shared-ui.cjs`/`stage-wheel-runtime.cjs`/`tauri-bundle.cjs` 并新增
  `resolve-target.cjs`；C1a 已新增 `contract/native-host/native-host.v1.json` 和
  request_http launch/contract 改动。
- 等三个提交回来后逐项复核；不直接把未提交工作树当成交付。

### 2026-11-13 12:55 CST

- C1a 完成：`feat/native-host-contract-20261113` → `431012be3`，已 rebase 到
  `origin/dev 0415ddfad` 并推送；committer 已修正为包装线负责人本人。
- C1a 验证：`tests/shell/request_http` **141 passed**；launch 专项 **20 passed**；
  `contract/http-request/validate.py` PASS；结构/模块门 PASS。
- C1a 未测：Windows ACL/reparse/fsync 实际行为（UNKNOWN）；`requiredPhases` 的
  `ready` 标签需 backend owner 确认；C1b 未开始。
- W3 基础提交 `04709c286` 已复核，发现缺 runtime contract preflight 与 `uv lock --check`；
  已发回子代理补这两项，暂不合并。
- W1 仍在运行；`setup-ui/` 与 native 接线已有未提交改动。

### 2026-11-13 13:10 CST

- W1 完成并合入 A：`da640e32a`（cherry-pick `ba609f663`）；setup 页为 plain ES modules，
  主窗口初始 `setup/index.html`，`is_shell_surface` + `SetupEnter` 门。
- 集成验证：setup 单测 29 passed；install-setup-ui 7 passed；Rust **526 passed**（仍仅 1 个
  既有 Windows 路径失败）；边界门 PASS（104 changed paths）；结构/模块门 PASS。
- W1 未测：真实 Tauri webview 的 setup 渲染/同意/enter/启动端到端（UNKNOWN）。
- W3 跟进仍在运行（runtime contract preflight + `uv lock --check` + stale dist 拒绝）；
  C1b 仍在运行。

### 2026-11-13 13:35 CST

- W3 完成并合入 A：`736e5d260`（基础 `--target`）+ `4b1e00d6c`（lock/contract preflight，
  stale dist 改为每次从 target 源码重建）。
- W3 验证：targeted 86 tests → **85 pass / 0 fail / 1 skip**；`build-release --target origin/dev
  --dry-run` exit 0，未创建输出目录；边界门 PASS（114 changed paths）；结构/模块门 PASS。
- full packaging suite：266 tests → **261 pass / 4 fail / 1 skip**；4 个失败为基线既有
  （cjk font、dmg-wiring ×2、payload-lock），与 W3 改动无关。
- W3 UNKNOWN：真实 Mac/Windows Tauri bundle、真实 `uv run --with <wheel>` 启动、真实
  `uv lock --check` 未跑；C1 target 当前没有 launch proof，预检会 fail closed。
- C1b 仍在运行；下一步：C1b 合并后跑“C1 target + setup + --target”端到端。

### 2026-11-13 13:55 CST

- A 侧 republish 接管（W4 前置）已接线：`HostState.upstream` 改为
  `Arc<tokio::sync::RwLock<Upstream>>`；每次 session/relay 前短暂写锁调用
  `Upstream::rebind()`，再读锁执行请求。`gui_host::start` 接受共享句柄，`boot.rs` 包装。
- 验证：`cargo check`/`cargo test --lib --features power-pack` → **526 passed**，仍仅 1 个
  既有 Windows 路径失败；`cargo fmt --check` PASS。
- 仍未验证：C1b 真实 republish 后 A 的 GUI 页面会话/catalog 连续性；`power_pack`
  promotion watcher 是否会在 external promotion 时误重启（需 C1b + 真机联调）。

### 2026-11-13 14:05 CST

- 修复 rebind 的 owner 过渡：bootstrap 启动时 `owner=None`，C1b promotion 后
  `server.json` 变为 `owner=Some`；`read_authority(..., allow_owner_transition)` 允许
  同一 launch nonce/pid 下从无主过渡到已验证 owner，`Upstream.owner` 随之更新。
- 验证：Rust **528 passed**，仍仅 1 个既有 Windows 路径失败；新增 owner transition 单测与
  `Upstream::rebind` 真实子进程测试（地址/token/generation/owner 全部替换）。
- A 的 local-gui config 现在声明 `nativeHost: {promotion: "external", contractVersion: 1}`。
  这意味着新打包线的目标必须是含 C1b 的后端；不含 C1b 的旧目标会因 unknown config key
  fail closed（新线不再承诺旧 target 兼容）。
- **待 C1b 提交后验证**：C1b 的 `config._native_host` 解析、`serve_external` 分支、
  initialize/promotion/republish、A 的 rebind/owner transition 端到端。

### 2026-11-13 14:25 CST

- 并行工作流扩展：
  - W5：C2a 平台（PTY/ACL/ports/process）+ C2b harness CLI 从 15c0b3707 移植，
    独立 worktree `feat/c2-platform-harness-20261113`（基于 C1a）。
  - W6：真 target preflight 的隔离 daemon/fake backend，独立 worktree
    `feat/preflight-daemon-20261113`（基于 A HEAD）。
- C1b 的全量 suite 仍在后台；尚未提交。三个子代理并行，互不共享工作树。
- W5/W6 完成后先各自复核，再按依赖顺序合并；C1b 仍是正式包的关键路径。

### 2026-11-13 15:00 CST

- C1b 已完成并推送：`d8fed9925`（在 C1a `431012be3` 之上，已 rebase 到 `origin/dev 0415ddfad`）。
  内容：external promotion、post-reply、generation/owner republish、bootstrap `auth.logout`、
  contract lifecycle true、OpenAPI 重生。
- 验证：`tests/shell/request_http` + onboarding focused = **253 passed**；结构/模块门 PASS；
  HTTP contract validate PASS；ruff PASS。
- 未验证：Full pytest 未由子代理完成；C1b 测试用 FakeHost，尚未与 A 的真实 rebind/
  session 连续性端到端联调；Windows ACL 仍 UNKNOWN。
- `C1-REVIEW.md` 已更新，等待 backend owner 复核；不自行合入。
- W5 C2a+C2b、W6 真 target preflight 仍在运行。

### 2026-11-13 15:20 CST

- W6 完成并合入 A：`48ec0d6be`。`preflight-target-contract.cjs` 现在从 target wheel
  启动一个隔离 daemon（temp HYPRIAL_HOME/state/user、短 socket），等 ping/owner 后
  再启动 target request_http，断言 launch proof + catalog；失败清理 daemon/request-http。
- 验证：preflight/target 测试 **39 passed**；边界门 PASS（115 paths）；结构门 PASS。
- 仍 UNKNOWN：尚未用真实 C1 wheel 跑通 isolated target daemon（测试用 isolated-daemon
  fixture）；真实 target 是否能在无登录/无网络的隔离 home 启动 owner，需要 C1 wheel 联调。
- W5 C2a+C2b 已提交 `23048a122`，仍在其 worktree 跑 full suite；待完成后复核/rebase。

### 2026-11-13 15:35 CST

- W6 真 target preflight 用**真实 C1a wheel** 跑通两次：`preflight-target-contract.cjs`
  启动 target wheel 的隔离 daemon（temp home/state/user/socket）→ 启动 target
  `request_http` → 断言 `launchNonce`/`hostContractVersion`/hostContract/phases/methods。
  结果 `ok:true`，`daemon.kind=isolated-target-wheel-daemon`，清理后无残留 root/进程。
- 限制：该 wheel 是本地验证构建（C1a `431012be3`，`HYPRIAL_BUILD_ALLOW_GLIBC_RAISE=1`，
  manylinux_2_34），**不可发布**；Windows named-pipe、macOS POSIX 未实测；C1b wheel
  尚未重跑；完整 `buildTargetRelease` 成功路径仍 UNKNOWN。
- 测试：preflight/target 39 passed；边界门 PASS；结构/模块门 PASS。

### 2026-11-13 15:50 CST

- W5 C2a+C2b 完成：`feat/c2-platform-harness-20261113` @ `23e469f63`（已 rebase 到 C1b
  `d8fed9925` 并推送）。内容：PTY/ACL/ports/process、harness CLI、managed_harness、
  Node 准备、CLI 命令、测试。
- 验证：C2 组合 focused suite **546 passed / 1 skipped**；与 15c0b3707 逐字节一致；
  结构/模块/ruff PASS。full `pytest tests/` 15 分钟超时（EXIT=124），**不是 PASS**。
- 未验证：Windows PTY/ACL/ConPTY、macOS POSIX、真实受管 harness 安装（UNKNOWN）。- `C2-REVIEW.md` 已创建；建议 C1 先审，C2 后审，不自行合入。
- Feishu 阶段 5 直发两次 `IPC_TIMEOUT`（本地 daemon message.send 无响应），未送达；
  下次 daemon 恢复后重试，不把 accepted 当已送达。

### 2026-11-13 16:00 CST

- 用 **C1b 真实 wheel**（`0.5.5.4+gd8fed99`，本地验证构建，不可发布）跑 W6 preflight：
  **PASS**。输出 `hostContractVersion=1`、`features.externalPromotion=true`、
  `requiredPhases=[bootstrap,ready]`、required methods 全 callable；
  `daemon.kind=isolated-target-wheel-daemon`、`ownerSeen=true`、清理后无隔离残留。
- 仍未验证：Windows/macOS、完整 `buildTargetRelease` 到 Tauri bundle 的成功路径。

### 2026-11-13 16:10 CST

- C2 最终验证：组合 focused suite **546 passed / 1 skipped**；与 15c0b3707 逐字节一致；
  结构/模块/ruff PASS；full pytest 15 分钟超时（EXIT=124），不算通过。
- 已启动一次完整 `buildTargetRelease`（Linux host）：target = C1b worktree，
  `--wheel` = C1b 本地验证 wheel，`--uv` pinned；目标目录 `/tmp/hyprial-c1b-pkg`。
  当前在后台运行，结果出来前不标记 PASS。
- C1/C2 分支已推送：`d8fed9925` / `23e469f63`；A HEAD `fd4bdf644`。

### 2026-11-13 16:25 CST

- 第一次完整 `buildTargetRelease`（Linux/C1b target）**失败于宿主依赖**：
  `Cannot find module '@tauri-apps/cli/tauri.js'`；这不是代码缺陷，是 A 工作树未安装
  `clients/native` Node 依赖。已执行 `npm --prefix clients/native ci`（2 packages），
  然后重跑同一构建；结果待回。第一次失败已写 `build-failure.json`，无半包。

### 2026-11-13 16:40 CST

- 第二次完整构建到达 Tauri bundle 后失败：AppImage 的 linuxdeploy 改写了 `runtime/uv/uv`
  （deb 的 uv 仍正确），product 路径漏掉 `repairLinuxAppImage`，导致 verifyBundle 的 uv
  digest mismatch。已修复：product 路径在 verify 前调用 `repairLinuxAppImage`，并把
  `appImageRepack` 记入 verified/provenance。
- 修复提交后重跑；上一次失败仍写 `build-failure.json`、无半包，未标 PASS。

### 2026-11-13 16:55 CST

- 用户决定：研发期间不串行等 full pytest；改为 **focused suites + 并行工作流**，
  full suite 只在集成/CI 阶段集中跑，问题批量修。不把 full suite 作为每个改动的门。
- 新并行工作流 W7：在 `intg/2727-c1-20261113`（基于 `origin/pr-2727` 33ec5390）上
  集成 C1a+C1b+C2，解决 request_http/onboarding 冲突，保留 2727 GUI/业务；只做本地
  打包集成分支，不 push/PR/merge。
- 完整 Linux 构建仍在上一次重跑中（Rust 编译/打包阶段），结果待回。

### 2026-11-13 17:05 CST

- 用户要求：**中期不跑 full pytest suite，不占本地资源**；改为 Mini 节点 worker 或 CI
  集中跑。已通过 Hyprial 网络委派 `mini-backend-worker` 跑 C1/C2 full pytest，
  messageId `d9607525-b86c-59b6-b094-5b3e5d99be97`，不 push/PR/merge。
- 本地只保留 focused suites；当前仅有一个已启动的完整 Linux 包构建在收尾，之后不再
  本地跑 full suite/重型全量。
- C1/C2 的 full suite 状态仍为“Mini 执行中/待回”，不是 PASS。

### 2026-11-13 17:20 CST

- 完整 Linux 构建第三次失败：`repairLinuxAppImage` 的 core 路径要求 Pi digest，product
  payload 没有 Pi，报 “prepared runtime, not build-enriched verified runtime”。已给
  `repairLinuxAppImage`/`validateVerifiedRuntime`/`preflightRuntimeMirror` 增加
  `requirePi` 选项；product 路径用 `requirePi:false`，repack 用 product `verifyBundle`。
- 修复后重跑完整构建；仍未标 PASS。

### 2026-11-13 17:35 CST

- 重型验证分流：
  - full pytest C1/C2 → Mini `mini-backend-worker`（`d9607525…`）。
  - macOS 验证包 → Mini `mini-packaging-worker`（`da575409…`）；当前
    `target-not-visible`/node-offline，已排队，待 Mini 节点恢复。
  - Windows 验证包 → local `local-windows-worker`（`6158bccb…`），accepted，使用
    A `8e3808b31` + C1b `d8fed9925`，不发布/不合入。
- 本地只保留一次 Linux 完整包重跑（AppImage product repair 修复后），结果待回。

### 2026-11-13 17:50 CST

- **完整 Linux `buildTargetRelease` PASS**（BUILD_EXIT=0）：
  - target C1b `d8fed9925`，host A `8e3808b31`，payloadVersion `0.1.3-r999`
  - installers：`/tmp/hyprial-c1b-pkg/Hyprial Desktop Prototype_0.1.3_amd64.deb`
    （sha256 `789b9026…`）、`…AppImage`（sha256 `b92008f0…`）
  - preflight：`hostContractVersion=1`、`externalPromotion=true`、isolated daemon
    `ownerSeen=true`；AppImage 已 repack，digest 读回一致
  - verifyBundle 通过；`build-provenance.json` 记录 host/target/双坐标/preflight/appImageRepack
- 限制：这是 **Linux/C1b 验证包**，不是 2727 GUI，不是可发布制品；Mac/Windows 仍待
  Mini/Windows worker。
- 本地不再跑重型全量；后续包验证交给 Mini/Windows。

### 2026-11-13 14:20 CST

- PR 2727 的 head 已从 `33ec5390` 前进到 `3b4e654a3`（+2 提交，非 `gui/` 部分只有
  docs/notes 与 GLOSSARY，未改 `src/hyprial/**` 或 `contract/**`）。`origin/pr-2727`
  已 fetch 到新 head；W7 集成分支需要 rebase 到新 head 才能代表“当前 2727”。
- 已通知 W7 代理在完成当前验证后 `git rebase --onto origin/pr-2727 33ec5390`，重跑
  focused；仍只本地、不 push/PR/merge。
- 阶段 6 飞书汇报已送达（messageId `3f5941fd-92b1-5dd7-9b89-4b48773dcacf`，
  native `om_x100b605eee1928a4cf55beba5952045`）。
- Windows worker 已于 14:01 CST 开始 turn（`6158bccb…`）；尚未回包。
- Mac worker（`da575409…`）仍 node-offline 排队；当前无 Mini 目标在线。
- 本地不再起第二个重型构建；等 2727 集成 rebase 完成后再打 2727 Linux 包。

### 2026-11-13 14:25 CST

- W7 集成分支已 rebase 到 PR 2727 新 head `3b4e654a3`：C1a `abedf9d3b`、C1b `cc6413425`、
  C2 `97e025a80`；`range-diff` 与 rebase 前逐字一致，工作树干净，`gui/` 0 改动。
- 为出包建独立 detached worktree `intg-2727-build-20261113`（HEAD `97e025a80`，clean），
  避免构建写入干扰集成分支工作树。
- 首次 2727 完整构建在 **wheel phase 失败**（BUILD_EXIT=1）：target `gui_build_hook`
  /`native_engine.platform_tag_for_floor` 要求 GLIBC_2.34，而声明 floor 是
  manylinux_2_17；不是 2727/C1/C2 代码缺陷，是本地宿主 glibc 限制。按既有本地验证
  口径加 `HYPRIAL_BUILD_ALLOW_GLIBC_RAISE=1`（manylinux_2_34，**不可发布**）重跑；
  `build-failure.json` 已记录 phase=wheel、targetCommit。
- 第二次构建后台运行（`bom96ua1o`，日志 `/tmp/hyprial-2727-build.log`）。

### 2026-11-13 14:35 CST

- **W7 集成完成**：`intg/2727-c1-20261113` @ `97e025a80`（base `3b4e654a3`；C1a
  `abedf9d3b`、C1b `cc6413425`、C2 `97e025a80`），工作树干净；已 push 到 origin
  （`ls-remote` 核验 `97e025a80`）。本分支只是本地验证集成分支，无 PR、未合入。
- 冲突处置（首次在 `33ec5390` 集成时）：`__init__.py` 保留 2727 的 `serve_gui` 并新增
  C1b external 分支；`app.py` 采用 C1b `after_reply_response`（与 2727 等价）；
  `bootstrap/response.py` 取 C1b 超集；`gui_host/service/runtime/specs/state` 取 C1b
  规范版（与 2727 差异仅为格式/等价表达，2727 onboarding 测试全过）；OpenAPI 重新生成，
  与 2727 基线逐字节一致（自动合并曾误带入 dev-only `message.relay.verify`，已避免）。
- 测试（rebase 后，Python 3.12）：request_http+onboarding focused **280 passed**；
  C2 platform/harness focused **219 passed / 1 skipped**（Windows-only 探针）；
  结构门/模块门 PASS；同样的套件在 3.14 也通过。
- **观察（待 backend owner 决策）**：C2 提交没有把 `contract/native-host/native-host.v1.json`
  与 `catalog.py` 的 `features.harnessCli`、`features.windowsPrivateAcl` 从 false 翻成
  true。集成分支可用，但宿主按能力位启用的 harness 旅程仍会被门住；翻转前需要 Windows
  真机验证 + owner 同意，不在本线程擅自改契约。
- UNKNOWN：真实 Windows ACL/ConPTY/Node 准备；full `pytest tests/`；A 宿主 + C1b 真实
  external promotion 端到端；GUI 测试未跑（相对新 base 无 GUI 文件改动）。
- 已给 Windows worker 追加第二个任务：在 A+C1b 包之后构建 2727 集成包
  （`79031ae5-fabe-5c84-b09d-77cdf92cf557`）。

### 2026-11-13 14:30 CST

- **2727 集成目标包 Linux 构建 PASS**（BUILD_EXIT=0）：
  - target `97e025a80`（PR 2727 head `3b4e654a3` + C1a/C1b/C2），target tree
    `02f051f0…`、guiTree `5e634498…`；host `7850bf3b5`（tree `d3f90062…`）
  - wheel `hyprial-0.5.5.4+g97e025a-cp312-abi3-manylinux_2_34_x86_64.whl`
    （sha256 `c794f403…`，27,406,138B）；payloadVersion `0.1.3-r999`
  - deb 83,800,380B sha256 `01807a66114c6a34f5bfcf90cf092ac688b8a923417f072338e3401259af0271`
  - AppImage 158,874,104B sha256 `973024fd069b2c360280583027e69e17978d5dcb142ce0b9cb263519e15bd18e`
  - preflight：`uv lock --check` ok、contract ok、`launchNonceSeen=true`、
    `hostContractVersion=1`、features `{externalPromotion:true, harnessCli:false,
    windowsPrivateAcl:false}`、isolated daemon `ownerSeen=true`/`phase=serving`、
    catalogDigest `sha256:a12d4174…`；guiDist 来源 `target-build`（treeDigest
    `148c3ce8…`）；AppImage repack 记录在 `verified.appImageRepack`
  - 静态复核：AppImage 可解包；wheel 内 `hyprial/_gui/release.json` 的
    `sourceCommit=97e025a80…`、103 个 GUI 文件，确认包内是 2727 目标的 GUI
- 限制：Linux 本地验证包，wheel 为 manylinux_2_34（`HYPRIAL_BUILD_ALLOW_GLIBC_RAISE=1`），
  **不可发布**；Windows/macOS 未覆盖；真实 C1b promotion 到 GUI 页面会话连续性仍未
  端到端验证；harness 能力位仍 false。
- 构建期间 A 工作树的后续提交只改状态文档（`19f9f2162`），provenance 记录的是构建启动时
  的 host `7850bf3b5`（dirty=false）；包内无文档内容，不影响制品。

### 2026-11-13 14:45 CST

- 用户要求恢复 Mini 节点。诊断：Mini 主机与网络正常（`api.hyprial.ai` 200、
  `code.hyprial.com` 200、Tailscale 在线，节点地址已脱敏），但 hyprial daemon **没有在跑**。
  活动 home 是 `~/.hyprial-mini-r2`；`daemon-health.json` 最后写入 14:01，socket 不存在。
  之前的 full pytest 委派（13:56 fetched）很可能在 14:01 daemon 停止时被中断。
- 已在 Mini 执行 `hyprial daemon start`（pid 2012，version 0.5.5.3.3+gb7d56d6，
  owner 为包装线负责人，标识已脱敏）；当前 `restoring`，本地 `agent list` 显示 squire、
  mini-packaging-worker、mini-backend-worker、mini-gui-worker、gui-verifier 等 10 个 agent
  均 online；orgfs 正在重同步（部分 supplier no-reply 后已 recovered）。
- 已重新发送两个任务（目前 queued：`target-not-visible`/`node-offline`，等待 mesh
  presence 传播）：
  - macOS 2727 集成包 → `mini-packaging-worker`（`20004788…`）
  - C1/C2 full pytest 重跑 → `mini-backend-worker`（`a20a993a…`）
- 本机 `hyprial host list` 已能看到 Mini 节点（设备名已脱敏），但状态仍 `unknown`，
  `message targets` 尚不可见；继续等 presence 收敛，再确认派单送达。

### 2026-11-13 14:55 CST

- **Windows 两个包均 FAILED at wheel**（不是代码内容问题）：`gui_build_hook.py` 的
  clean-source 探针强制 `core.fileMode=true`；NTFS 不能表示 100755，干净 checkout 也把
  91 个脚本报成 100644，`git diff --name-only HEAD` 除 VERSION 外多出 91 条 mode-only
  条目，`_wheel_release_stamp_is_only_change()` 因而拒绝。两次都没有 installer/provenance。
- 修复分支 `fix/gui-wheel-windows-filemode-20261113` @ `9b06b76c3`（基于 `origin/dev
  c008113e3`）：POSIX 保持 fileMode=true；`os.name=="nt"` 用 fileMode=false（NTFS 无
  exec 权限语义，内容/索引/未跟踪守卫不变），文件头决策块记录理由。
  focused hook 测试 **57 passed**（新增 4 个：config 两平台、Windows 仅 exec-bit 可过、
  Windows 内容编辑仍拒）；结构/模块/ruff/inline-comment/contract-impact 均 PASS。
- Windows 重试用临时目标分支 `intg/2727-c1-winfix-20261113` @ `1f4aab6da`
  （= `97e025a80` + 上述修复 cherry-pick），已 push；worker 任务 `d8fa24e1…` 已接单。
  该分支只用于 Windows 验证包，不能当作 2727 目标本身；普通 2727 包仍以 `97e025a80`
  为准（Linux 包已是该 SHA）。
- Windows worker 前两次的回复没有到达 squire（daemon 日志有
  `send.sender_unauthenticated` / `reply_error`）；结果以它 workspace 的
  `STATUS-20261113-windows-packaging-verify.md` 与 evidence 目录为准。
- Mini：daemon 已恢复，Mac 2727 包与 C1/C2 full pytest 两个任务均已 fetched。

### 2026-11-13 14:55 CST

- **新 PR 已起**：
  - `#2734` C1（`feat/native-host-contract-20261113` → `dev`）：已 rebase 到 `dev
    c008113e3`，C1a `cf1807d80` + C1b `3301954a1`；focused **262 passed**，contract/
    结构/模块/ruff PASS。
  - `#2735` A（`build/packaging-host-20261113` → `dev`）：无需 rebase，`git merge-tree`
    对 `origin/dev c008113e3` 无冲突；PR 正文注明建议先合 C1（#2734）再合 A。
  - `#2736` Windows fileMode 修复（`fix/gui-wheel-windows-filemode-20261113` → `dev`）。
- **旧 PR 处理**：`#2637`（旧一体化打包线容器）已加说明并关闭（不合并）；分支
  `intg/gui-first-run-20261110` @ `15c0b3707` 与审核/包证据保留，不删除。新路径以
  #2734/#2735/#2736 + PR #2727 为准。
- C2 仍单独保留在 `feat/c2-platform-harness-20261113`；按 C1-REVIEW/C2-REVIEW 的建议，
  等 C1 审核后再起 C2 PR。
- 未合入任何 PR；Windows/Mac 包和 CI 结果待回。

### 2026-11-13 15:00 CST

- **macOS 2727 集成包 PASS**（Mini `mini-packaging-worker`，attempt 2）：
  - target `97e025a80`（tree `02f051f0…`、guiTree `5e634498…`）、host `7850bf3b5`
  - DMG `Hyprial Desktop Prototype_0.1.3_aarch64.dmg` 61,811,526B
    sha256 `19c2a6ecccaed7cc4ec5d90ff142fb92caf40c4fa0df803a9e1b18e052460013`；
    `hdiutil verify` exit 0（CRC32 VALID）
  - wheel `hyprial-0.5.5.4+g97e025a-cp312-abi3-macosx_13_0_arm64.whl`；
    preflight `hostContractVersion=1`、`externalPromotion=true`、隔离 daemon
    `ownerSeen=true`/`phase=serving`；`verifyBundle` valid，ad-hoc 签名、未 notarize
  - attempt 1 在 wheel 失败：`TailcatWheelError: no sidecar binary for darwin/arm64`
    （无 `HYPRIAL_TAILCAT_PREBUILT`、无 go 工具链）；attempt 2 用目标源码 + 本地 Go
    1.27.1 离线模块缓存重建 tailcat，经 `HYPRIAL_TAILCAT_PREBUILT` 传入，未改源码
  - 未做 mount/install/launch/login；GUI 旅程 UNKNOWN；证据
    `/Volumes/HyprialDev/packaging-evidence/mac-2727-integration-97e025a80-20261113/`
- **新 PR 的 pr-hygiene 已全绿**：#2734/#2735/#2736 的正文首行补
  `Submitted-by: agent:<owner>:<device>:squire`（真实 URI 已脱敏）后重跑，
  三个 PR hygiene 均 success；其余 CI 仍在跑。
- Windows 2727 重试（`intg/2727-c1-winfix-20261113` @ `1f4aab6da`）进行中，
  已到 pkg3 构建阶段；尚无结果。

### 2026-11-13 15:05 CST

- C2 分支已 rebase 到新 C1 head：`feat/c2-platform-harness-20261113` @ `94c9ae2ff`
  （base `3301954a1`），focused **219 passed / 1 skipped**，结构/模块/ruff PASS；已
  force-with-lease 推送，暂未开 PR（等 C1 #2734 审核）。
- 新 PR 的 CI：pr-hygiene/declarations 已绿；`gate-python / checks`、`cli-help-inventory`、
  `unit` 在三个 PR 上都红，且 PR #2727、#2732 同样红（#2731 的历史结果绿）。本地复跑
  `ci-scope/ci-plan` 156 passed、结构/模块/windows-portability/network/unbounded-waits
  PASS、`uv sync --locked --python 3.12` + `check_cli_help_inventory.py` PASS。倾向
  CI runner/代理侧故障（本机同时出现 tls handshake eof），**未归因、未宣称 CI 绿**；
  需要时重跑或由 CI owner 处理。
- Windows 2727 重试仍在构建（pkg3 只有 INPUTS.json）。

### 2026-11-13 15:10 CST

- **Mini daemon 反复停止的真实原因：磁盘满，不是网络**。daemon 日志：
  `daemon.home_lease.fenced` / `fenceReason: ownership-record-lost:OSError[ENOSPC]`，
  随后 `daemon.stopping stopRequestSource=home-ownership-lost`（14:01 与 14:51 两次）。
  Data volume 228Gi 仅剩 104Mi（100%）。
- 清理（保留证据）：删除 agent `state/xdg/cache`、各 agent home 的 `Library/Caches`、
  `~/Library/Caches/{uv,go-build,Google}`；把 `rebuild/worker-bootstrap-20261105` 与
  `mini-packaging-worker/workspace/evidence` 移到
  `/Volumes/HyprialDev/hyprial-mini-archive-20261113/`。Data volume 现在 **16Gi free**
  （92%）。
- 重新 `hyprial daemon start`：pid 33102、`restoring`；正在观察 home lease 稳定性与
  presence。上一次 full pytest 重发任务 `29c50537…` 因 Mini 不可见而 queued；
  等 presence 恢复后应自动送达。
- 另：Mini `mini-backend-worker` 上一 turn 以 `HARNESS_TRANSIENT_FAILURE`
  （Claude Agent SDK response ended without a terminal result）失败，不是测试失败。

### 2026-11-13 15:20 CST

- **Windows 2727 包 BUILD OK**（pkg3，attempt 3b）：
  - target `1f4aab6daefeb75d68092b0b4d87f44209be5167`（= 97e025a80 + Windows
    exec-bit 修复 cherry-pick；tree `4a108d70…`、guiTree `5e634498…`）、host
    `7850bf3b5`（tree `d3f90062…`，dirty=false）
  - installer `D:\hyprial-win-tools\packaging-verify-20261113-pkg3\out\Hyprial Desktop
    Prototype_0.1.3_x64-setup.exe`，60,514,515B，NSIS win-x64，sha256
    `26c968f195fc391a1c4950db30f905b4fbf99c1f1f7739e7313755728769b6d2`（WSL 侧
    `/mnt/d/...` 复核一致）
  - wheel `hyprial-0.5.5.4+g1f4aab6-cp312-abi3-win_amd64.whl`（26,888,192B，
    sha256 `18197bf0…`）；preflight `hostContractVersion=1`、`externalPromotion=true`、
    `transport=windows-pipe`、`ownerSeen=true`、phase serving；build exit 0（945s）
  - attempt 3a：wheel 阶段已过（确认 exec-bit 修复有效），contract 阶段因隔离 `TMP`
    继承了 `Authenticated Users:(M)`/`Users:(RX)`，request-http.json 不满足私有 ACL
    而被拒（CONFIG_INVALID）。处理：在 pkg3 下建 `tmp-private`（`icacls /inheritance:r`，
    仅当前用户/System/Administrators），把 `TMP`/`TEMP` 指过去重跑；**未改仓库/产品代码**。
- Windows 包与 Linux/macOS 包的 target 不同：Windows 必须带 winfix 提交（1f4aab6da），
  Linux/macOS 是 97e025a80。三者都不是可发布制品。

### 2026-11-13 15:30 CST

- 用户要求 Windows/macOS 两个 2727 包放在一个目录：已交付
  `D:\hyprial-2727-win-mac-20261113\`（WSL `/mnt/d/hyprial-2727-win-mac-20261113/`），
  含 `x64-setup.exe`、`mac/…aarch64.dmg`、provenance、报告、`SHA256SUMS.txt`、README；
  `sha256sum -c` 两个包 OK。zip 已按用户要求删除。
- Mini daemon 自 15:10 重启后保持 running（pid 33102），Data volume 14Gi free；
  之前“不稳定”告警是观察脚本把 status 里两个 `running: true` 数成 2 导致的误判。
- `mini-backend-worker` 的 full pytest 任务第一次 HARNESS_TRANSIENT_FAILURE 后，
  daemon 已自动重投（07:11:22Z 重新 turn.started）；目前未再报失败，继续等结果。

### 2026-11-13 15:55 CST

- **Windows 2727 包启动测试暴露的真实 blocker 已修**：
  - C1（#2734）`e80c7a208`：`serve_external` 在 `host.phase != bootstrap` 时改为普通
    serving（保留 launchNonce/stdin stop/hostContract），bootstrap 仍走 promotion；
    新增 `test_an_admitted_serving_host_serves_without_promotion`。focused **263 passed**。
  - A（#2735）`1a520fc33`：`report_service_failure` 不再 `show_main`，改为 hide+disable
    MAIN，只保留 startup 失败页（单窗口）。`cargo check` PASS；`cargo test --lib
    --features power-pack` 527 passed（1 个既有 Windows-only 路径断言、1 个并行 flake
    单跑通过）。
- 集成分支已追到 PR 2727 新 head：`intg/2727-c1-20261113` @ `fe84bf2e6` =
  `7af55eb9b` + C1a `1143a0a10` + C1b `89db28d7a` + C2 `f029d5ada` + C1 fix
  `fe84bf2e6`；request_http+onboarding **281 passed**，C2 **219 passed/1 skipped**，
  结构/模块 PASS。Windows 目标 `intg/2727-c1-winfix-20261113` @ `f67784cf3`
  （= `fe84bf2e6` + filemode fix），hook 测试 57 passed。
- Windows v2 包构建任务 `ca82989a…` 已发；macOS 重打包等 Mini full pytest 结束
  （Mini 上 8 个 pytest worker 自 15:41 运行，内存紧张，避免并发 OOM）。
- 包内坐标将更新为：macOS target `fe84bf2e6`、Windows target `f67784cf3`、
  host `1a520fc33`。

### 2026-11-13 16:05 CST

- **Mini full pytest 完成（C1，macOS arm64）**：`PYTEST_EXIT=1`，
  **99 failed / 26,952 passed / 156 skipped / 7 xfailed / 2 xpassed / 14 errors**，
  14m40s；日志与 XML 在
  `/Volumes/HyprialDev/HyprialOS/.smoke/c1-c2-full-pytest-20261113/run-20261113T0711Z/c1/`
  （`pytest.log` 180KB、`pytest.xml` 4.9MB）。
  **不是 PASS**；失败清单尚未分类，不能归因到 C1 或基线。C2 的 full suite 未跑。
- 按计划先出包；full pytest 的分类放到包验证之后（必要时用 dev 基线同机对照）。
- macOS v2 打包任务已发（`f14968ad…`，当前 pending，等 mesh 重投）；Windows v2
  （`ca82989a…`）构建中。

### 2026-11-13 16:45 CST

- **macOS v2 包已安装到 Mini 并启动测试**：app 进程、daemon、request-http 都在跑，
  request-http 日志有成功的 onboarding.status/agent.list/org.list，`last-startup.json`
  为 ready，`startup-error.json` 不存在；但 GUI 一直停在“正在连接本机服务”。
- 诊断：v2 的 returning-user fallback 发布的 `server.json` 是 bootstrap 形状
  （`phase`/`transportGeneration`/`owner` 全空），GUI 的 `/daemon/session` 握手把它当
  bootstrap，等待 owner/promotion，所以一直重连。
- 修复 C1 `e3c1c5533`（PR #2734）：非 bootstrap host 走 serving 时，用已验证 owner
  旋转 config，发布 `capability_phase=ready` + 新 `transportGeneration`，principal 带
  owner/actor；回归测试断言 ready/owner/generation。focused **263 passed**。
- 新目标：`intg/2727-c1-20261113` @ `34f6d9487`、Windows `intg/2727-c1-winfix-20261113`
  @ `e2f3946a0`；macOS v3 打包任务 `8b559798…`（pending），Windows v3 `e1e2654d…`
  （排在 v2 后）。v2 包作废，不交付。

### 2026-11-13 17:00 CST

- Mini v2 热补 ready-session 修复后复测：`server.json` 已是 `phase=ready` +
  owner + transportGeneration，GUI 能进入，但**仍反复弹“正在连接本机服务”**。
- 第二个根因：2727 GUI 的 same-window watcher 每秒 `POST /daemon/session/status`
  期望 `{ok:true,current:boolean}`；A 宿主只实现了 `/daemon/session` 与
  `/daemon/session/logout`，每次 404 触发 reconnect overlay。
- A 修复 `a3156e3f8`（PR #2735）：新增 `/daemon/session/status`，校验 business
  cookie + `x-hyprial-view` + `x-hyprial-generation`，有效返回 current:true，
  generation 过期返回 current:false；`cargo check`/fmt PASS，gui_host 测试 36 passed。
- v4 打包：host `a3156e3f8`，macOS target `34f6d9487`、Windows target `e2f3946a0`；
  v2/v3 包作废、不交付。Mini 上的 v2 app 已退出，等 v4 安装。
- 效率结论：后端可用 venv 热补 + 重启（1–2 分钟）；**原生宿主改动**仍需要一次
  app 构建（但可只做 debug 运行或只重打宿主，不必每次走完整安装包验收）。

### 2026-11-13 17:10 CST

- **macOS v4 已安装到 Mini**：DMG 61,810,817B sha256 `1587feb8…`；app 二进制
  sha256 `4c5d5800…` 与构建报告一致；host `a3156e3f8`、target `34f6d9487`；启动
  5 秒内 ready，无 `startup-error.json`；当前在 setup 页等用户点“进入 Hyprial”。
  v2/v3 包作废，不交付。
- Windows v4 正在 pkg4 下构建（日志 `61-v4b.log`，17:03 起）。
- 旧 C1a 代理补报（完成通知）：C1 分支 HEAD `e3c1c5533`；full suite 在 14% 停下
  （3 failed / 3906 passed / 1 error），其中 `FakeApp.state_dir` 两项与 dev 基线
  逐字节相同、属 dev 既有破坏，`qq token` 为全量并发 flake（单跑通过）——**不是
  C1b 回归**；与 Mini 上 99 failed 的分类方向一致（基线/环境为主，仍需完整分类）。
  另记：C1b 提交 `3301954a1` 的 committer 是 `agent-codex-operator`（author 仍是
  包装线负责人），由代理 rebase 带入，暂不改写已推送历史；logout 尚未 drain serving
  listener / 重写空 home bootstrap（只有 reopen_required + durable receipt），
  是已知缺口。

### 2026-11-13 17:15 CST

- **macOS v4 真机验证通过（连接层）**：清掉上一次强杀遗留的 orphan daemon/app-core 后重启
  v4，app/daemon/app-core/request-http 全部起来；`server.json` 为 `phase=ready` +
  `transportGeneration=gui-…` + owner 为包装线负责人（标识已脱敏）；GUI 正常轮询
  onboarding.status/org.list/agent.list/adapter.list；用户确认“进去了，目前正常了”，
  不再反复弹“正在连接本机服务”，无 startup-error。
- 两个根因均已覆盖：C1 `e3c1c5533`（returning-user 发布 ready session）与 A
  `a3156e3f8`（`/daemon/session/status`）。**这是连接层验证，不是整包产品验收**；
  聊天/登录/飞书/真实模型仍未实测。
- 观察：GUI 在 returning-user 会话下会调用一次 `onboarding.initialize`，request-http
  记 `unsupported_method / HTTP_CALL_PROFILE_UNSUPPORTED` 后继续正常轮询；不是启动失败，
  但值得在 2727 侧确认是否需要先探测 phase。
- 测试环境注意：硬杀 app 会留下 daemon/app-core 孤儿，下一次启动会因 runtime home 冲突
  显示“启动失败”；正常退出不会。已记录，非 v4 包缺陷。
- Windows v4 仍在上次 pkg4 目录构建（17:02 起 `61-v4b`）；v2 的 16:39 installer 不是 v4，
  不交付。

### 2026-11-13 17:25 CST

- Windows v4（host `a3156e3f8`、target `e2f3946a0`）attempt 1 在 `contract` 阶段失败：
  `CONTRACT_MISMATCH: target contract preflight failed: EBUSY: resource busy or locked,
  rmdir '...tmp-private-v4\hyprial-pf-…'`。这是 `preflight-target-contract.cjs` 在
  Windows 上 `taskkill /T /F` 后立即 `fs.rm` 的回收竞态，不是源码/契约断言失败；
  同一目标 v3 也遇到、v2（不同目标）通过。attempt 2（`61-v4b`）已用新空输出目录和
  新私有 TMP 重跑，正在编译 Rust。
- 该 EBUSY 竞态需在 A 的 preflight 里加删目录重试/等待进程退出（作为后续小修复，
  不阻塞本包；若 v4b 仍失败则先修再出 v5）。
- macOS v4（用户已验证正常的版本）就是交付版本；Windows v4b 成功后一起更新
  `D:\hyprial-2727-win-mac-20261113\`。

### 2026-11-13 17:35 CST

- **Windows v4b BUILD SUCCESS**（attempt 2）：target `e2f3946a0`、host `a3156e3f8`；
  NSIS installer 60,513,532B sha256
  `ecc042818a8d346ea2c3f666f95207a6dea76bda3434314435dbf68987fe0430`；
  wheel `0.5.5.4+ge2f3946`；preflight `hostContractVersion=1`、`externalPromotion=true`、
  `transport=windows-pipe`、`ownerSeen=true`。attempt 1 的 EBUSY 是 preflight 回收竞态，
  attempt 2 通过；该竞态仍需给 `preflight-target-contract.cjs` 加删除重试（后续小修）。
- **交付目录已更新为 v4**：`D:\hyprial-2727-win-mac-20261113\`
  - Windows `…_x64-setup.exe` 60,513,532B sha256 `ecc04281…`
  - macOS `mac/…_aarch64.dmg` 61,810,817B sha256 `1587feb8…`
  - `SHA256SUMS.txt` 两个包 `sha256sum -c` OK；README/两平台 provenance/报告已同步
- macOS v4 已在 Mini 安装并验证连接；Windows 尚未安装/启动。

### 2026-11-13 17:45 CST

- **登出后“本机服务连接超时/HTTP_NOT_READY/没有登录页”定位**（Windows/macOS 同因）：
  - A 原生宿主（`gui_host` 会话层）中继 `auth.logout` 后没有轮换/撤销 GUI 会话，也没有
    重新发布空 home 的 bootstrap authority；GUI 的 same-window watcher 等不到
    `/daemon/session/status` 的 current:false，30 秒后超时；重连又撞到旧的 ready
    authority，宿主返回 HTTP_NOT_READY。GUI 按契约做了 logout transition，不是 GUI 逻辑
    错误；打包脚本/载荷也不是原因。
  - C1b 已知缺口：logout 只返回 `loggedOut/nextAction=login` + `reopen_required`，
    没有 drain serving listener 并重发 bootstrap（C1 代理也报告了这一点）。
- 修复：
  - 集成分支 `d87577360`（+ 测试适配 `1250e90d0`）：returning-user 的 external 配置改为
    复用 2727 的 `serve_gui` 生命周期（传入 launch_nonce + stdin stop），logout 时
    `serve_gui` 关闭旧 server、重开空 home host 并重新发布 bootstrap。
  - A `d8fd718bc`：`HostState::upstream()` 在 owner/transportGeneration 变化时
    `retire_all_business()`（保留 control+view）；`/daemon/session/status` 在业务会话
    已退役但 control+view 仍有效时返回 `{ok:true,current:false}`，GUI 随即用 control
    刷新到 bootstrap 会话（登录页）。新增 session 测试，gui_host 37 passed；cargo check PASS。
  - 注意：logout 重开依赖 B 的 `serve_gui`，这是 B+C1 集成逻辑，最终合入需要一个
    可合入落点（C1 自包含实现或 B 侧随 2727 合入），当前先用于 v5 验证包。
- v5 构建：host `d8fd718bc`；macOS target `1250e90d0`、Windows target `60a3eaf28`；
  集成 focused **281 passed**。v4/v2/v3 包作废。

### 2026-11-13 17:55 CST

- **macOS v5 BUILD SUCCESS**：DMG 61,811,406B sha256
  `cd7cb6945389abec6afb9f77d50e4b8df74ffe9908698038582016ad50754ea1`；
  target `1250e90d0`、host `d8fd718bc`；app 二进制 `4ba7db05…`。已替换 Mini 上的 v4
  并启动，等用户点“进入 Hyprial”后验证登出→登录页。
- Windows v5 构建已启动（`70-v5` 日志）；v4 及更早包作废，不交付。
- v5 的登出修复 = intg `d87577360`/`1250e90d0`（serve_gui logout reopen）+
  A `d8fd718bc`（authority 变化时退役业务会话，status current:false）。真实登出旅程
  待用户验证；合入落点仍需 B+C1 归属确认。

### 2026-11-13 18:05 CST

- Windows v5 attempt 1 再次在 contract 阶段撞到预检 EBUSY（`tmp-private-v5\
  hyprial-pf-4EwDuJ`），与 v3/v4 同一竞态；不是源码/契约失败。已请求 stock retry
  （`out-v5b`/`tmp-private-v5b`，`71-v5b` 已在构建）。
- A 修复 `0582887e0`：`preflight-target-contract.cjs` 的 `stopChild` 在 Windows
  taskkill 后等待子进程释放，`removeRoot` 对 EBUSY/EPERM/EACCES/ENOTEMPTY 重试；
  preflight target 测试 **29 passed**。该修复在 A 分支上；如果 v5b 仍失败，用
  host `0582887e0` 出 v6（构建宿主脚本带重试）。
- macOS v5 已安装，等用户验证登出→登录页。

### 2026-11-13 18:15 CST

- **Windows v5b BUILD OK**（stock retry）：target `60a3eaf28`、host `d8fd718bc`；
  NSIS installer 60,527,314B sha256
  `5574c9295574fc3762e05b8ba1aee8f0b6d3b7d47261d460b64c1f23765fd38a`；
  preflight `hostContractVersion=1`、`externalPromotion=true`、`windows-pipe`、
  `ownerSeen=true`。
- **交付目录已更新为 v5**：`D:\hyprial-2727-win-mac-20261113\`
  - Windows `…_x64-setup.exe` 60,527,314B sha256 `5574c929…`
  - macOS `mac/…_aarch64.dmg` 61,811,406B sha256 `cd7cb694…`
  - `SHA256SUMS.txt` 两个包 `sha256sum -c` OK；README/两平台 provenance/报告已同步
- macOS v5 已安装到 Mini（v4 替换），登出→登录页待用户验证；Windows 未安装。
- A 的 EBUSY 预检重试修复 `0582887e0` 已在 A 分支（29 tests PASS），但**不在本包**
  （本包是 stock retry 成功产物；后续 Windows 构建会带该修复）。

### 2026-11-13 18:30 CST

- **已正式申请合入 dev**（不自行合入）：
  - #2734 C1（`feat/native-host-contract-20261113` @ `e3c1c5533`）
  - #2735 A（`build/packaging-host-20261113` @ `3db5160f4`，建议在 C1 之后）
  - #2736 Windows fileMode 修复（`9b06b76c3`，独立可先合）
  - #2741 C2（`feat/c2-platform-harness-20261113` @ `f43680a67`，**stacked** 在
    #2734 分支；C1 合入后 retarget 到 dev）
  - 四个 PR 都已请求 `allenwoods` 审核，并附上验证/风险说明。
- 合入顺序建议：#2736 → #2734 → #2735 → #2741（C2 retarget dev）；PR 2727（B）由
  其作者自行同步后合。
- **CI 注意**：四个 PR 的 `checks`/`cli-help-inventory`/`unit` 红是 dev #2728 基线
  问题；dev `0d8ee9c07`（#2739 baseline sync）已更新基线，需在**当前 dev 基线**上
  重跑精确 head 的线上 CI，旧 run 不能当结论。
- **登出收口**：v5 包的 logout→登录页依赖 B 的 `serve_gui` 集成 glue（intg
  `1250e90d0`），不在 #2734；C1+A 先合、B 未合时 external 配置仍是 serving 回退
  （不弹登录页但不崩）。需要 B/2727 或后续 C1 follow-up 收口。
- C2 的 `harnessCli`/`windowsPrivateAcl` 仍 false，Windows 真机验证仍 UNKNOWN。

### 2026-11-13 18:45 CST

- 按 Allen 要求合成单一集成分支：`intg/packaging-host-backend-20261113`（从
  `origin/dev 0d8ee9c07` 起）：
  - merge A `build/packaging-host-20261113` @ `738957149`
  - merge C1 `feat/native-host-contract-20261113` @ `e3c1c5533`
  - merge C2 `feat/c2-platform-harness-20261113` @ `f43680a67`
  - merge Windows filemode `fix/gui-wheel-windows-filemode-20261113` @ `9b06b76c3`
  - 全部 `--no-ff`、无冲突；集成 HEAD `d57677ed5`。
- 验证：组合 focused Python **482 passed / 1 skipped**；Rust **529 passed / 1 failed**
  （既有 Windows-only 路径断言）；结构/模块 PASS；A 边界门对组合分支
  `not-applicable`（A-only #2735 的门保持有效）。
- **PR #2742** 已创建（base `dev`，head `intg/packaging-host-backend-20261113`），
  已请求 `allenwoods` 审核；#2734/#2735/#2736/#2741 已加说明并关闭，统一由 #2742
  审核/合入。
- 本 PR 不含 B（2727）；登出→登录页的 `serve_gui` 集成 glue 不在本分支；C2 能力位
  仍 false；CI 需在 dev `0d8ee9c07` 基线上重跑。

## 5. 测试与证据

| 命令 | 结果 | 时间 |
|---|---|---|
| `cargo test --lib --features power-pack` | **528 passed**；1 个既有平台失败（`runtime::env::tests::windows_product_path...` 仅大小写差异，Linux 上的 Windows 路径断言） | 2026-11-13 14:15 |
| `cargo fmt --check` | PASS | 2026-11-13 11:59 |
| `pytest tests/tooling/packaging_host/test_check_packaging_host_boundary.py` | **7 passed** | 2026-11-13 11:52 |
| `ruff check`（新脚本+测试） | PASS | 2026-11-13 11:52 |
| `check_structural_limits.py` | PASS（violations=[]） | 2026-11-13 11:58 |
| `check_module_boundaries.py` | PASS | 2026-11-13 11:58 |
| `check_packaging_host_boundary.py --base origin/dev` | PASS（88 changed paths，0 findings） | 2026-11-13 12:02 |
| C1a `pytest tests/shell/request_http` | **141 passed**（含 20 launch tests） | 2026-11-13 12:53 |
| C1a `contract/http-request/validate.py` | PASS | 2026-11-13 12:54 |
| C1a 结构/模块门 | PASS | 2026-11-13 12:54 |
| C1b focused suite | **253 passed**（request_http + onboarding + runtime） | 2026-11-13 14:55 |
| C1b contract/结构/模块/ruff | PASS | 2026-11-13 14:54 |
| W6 preflight/target tests | **39 passed** | 2026-11-13 15:10 |
| W6 真实 C1a wheel e2e | **PASS ×2**；isolated daemon + launch/catalog contract；清理无残留 | 2026-11-13 15:25 |
| W5 C2 focused suite | **546 passed / 1 skipped**（组合 focused） | 2026-11-13 15:55 |
| W5 C2 结构/模块/ruff | PASS；与 15c0b3707 逐字节一致 | 2026-11-13 15:55 |
| W5 full pytest | **EXIT=124 超时，不是 PASS**；需安静机器重跑 | 2026-11-13 15:58 |
| **C1b 真实 wheel preflight** | **PASS**：hostContractVersion=1、externalPromotion=true、isolated daemon ownerSeen=true | 2026-11-13 16:00 |
| **Linux 完整包 buildTargetRelease** | **PASS**：deb 83,756,630B / AppImage 158,816,760B；verifyBundle + provenance 通过 | 2026-11-13 17:50 |
| **2727 集成目标包 buildTargetRelease（Linux）** | **PASS**：target `97e025a80`、host `7850bf3b5`；deb 83,800,380B / AppImage 158,874,104B；preflight ownerSeen | 2026-11-13 14:29 |
| Windows A+C1b / 2727 目标包 | **FAILED at wheel**：exec-bit-only dirt 被 clean-source 探针拒绝；无 installer/provenance | 2026-11-13 14:30 |
| **macOS 2727 集成包** | **PASS**（attempt 2）：DMG 61,811,526B sha256 `19c2a6ec…`；hdiutil verify；preflight ownerSeen | 2026-11-13 14:50 |
| PR #2734/#2735/#2736 pr-hygiene | 三个均 **success**（补 Submitted-by 后） | 2026-11-13 15:10 |
| Windows fileMode 修复 focused（3 个 hook 测试文件） | **57 passed**；结构/模块/ruff/inline/contract PASS | 2026-11-13 14:50 |
| W7 request_http + onboarding focused（3.12） | **280 passed** | 2026-11-13 14:19 |
| W7 C2 platform/harness focused（3.12） | **219 passed / 1 skipped** | 2026-11-13 14:22 |
| W7 结构门 / 模块门 | PASS（failing 0 / violations []） | 2026-11-13 14:23 |
| W3 targeted Node tests | **85 passed / 0 failed / 1 skipped** | 2026-11-13 13:30 |
| W3 full packaging suite | 261 passed / 4 failed / 1 skipped（4 个为基线既有） | 2026-11-13 13:32 |
| W3 `build-release --target origin/dev --dry-run` | exit 0；未创建输出目录 | 2026-11-13 13:31 |
| A 集成边界门 | PASS（114 changed paths） | 2026-11-13 13:30 |

## 6. 阻塞/风险

- C1 需要 backend owner 审核；本线程只起草，不自行合入。
- 2727 的 `serve_gui` 与 A 生命周期所有权冲突；C1 抽契约，不原样搬。
- Mac/Windows 构建主机单槽；真实包验证需排期。
- **W3/W6 target 预检**：W6 已用真实 C1a 和 C1b wheel 跑通（隔离 daemon + launch/catalog
  断言）；C1b wheel 输出 `externalPromotion=true`、ownerSeen=true。两颗 wheel 都是本地验证
  构建、不可发布；Windows named-pipe/macOS POSIX 与完整 `buildTargetRelease` 成功路径仍
  UNKNOWN。正式包前必须在目标平台重跑，不得把当前 PASS 当作发布验收。
- **A 侧 republish 接管（已接线，待端到端）**：`HostState.upstream` 已改为
  `Arc<tokio::sync::RwLock<Upstream>>`，每次 session/relay 前写锁 `Upstream::rebind()`，
  owner transition 也有单测和真实子进程测试。仍 UNKNOWN：C1b 真实 republish 后 GUI
  页面已有会话/catalog digest 的连续性，以及 power-pack promotion watcher 是否会在
  external promotion 时误重启；需要 2727 + C1b 真机联调。
- **W7 集成分支基线**：已 rebase 到 PR 2727 head `3b4e654a3`，当前 HEAD `97e025a80`
  （已 push）。PR 2727 还会前进，出正式包前必须重新 fetch 并 rebase。

## 7. 2727 验证包清单（2026-11-13）

| 平台 | target | host | 产物 | 字节 | sha256 |
|---|---|---|---|---|---|
| Linux | `97e025a80` | `7850bf3b5` | deb `Hyprial Desktop Prototype_0.1.3_amd64.deb` | 83,800,380 | `01807a66114c6a34f5bfcf90cf092ac688b8a923417f072338e3401259af0271` |
| Linux | `97e025a80` | `7850bf3b5` | AppImage `…_amd64.AppImage` | 158,874,104 | `973024fd069b2c360280583027e69e17978d5dcb142ce0b9cb263519e15bd18e` |
| macOS arm64 | `97e025a80` | `7850bf3b5` | DMG `…_aarch64.dmg` | 61,811,526 | `19c2a6ecccaed7cc4ec5d90ff142fb92caf40c4fa0df803a9e1b18e052460013` |
| Windows x64 | `1f4aab6da`（+winfix） | `7850bf3b5` | NSIS `…_x64-setup.exe` | 60,514,515 | `26c968f195fc391a1c4950db30f905b4fbf99c1f1f7739e7313755728769b6d2` |

路径：Linux `/tmp/hyprial-2727-pkg/`；macOS
`/Volumes/HyprialDev/packaging-evidence/mac-2727-integration-97e025a80-20261113/artifacts/`；
Windows `D:\hyprial-win-tools\packaging-verify-20261113-pkg3\out\`（WSL 可见 `/mnt/d/…`）。

限制：全部是验证包，不可发布。Linux wheel 为 manylinux_2_34（`HYPRIAL_BUILD_ALLOW_GLIBC_RAISE=1`）；
macOS 为 ad-hoc 签名、未 notarize；Windows 未安装/未启动；三平台均未做真实登录/聊天 E2E
与 GUI 旅程验收；Windows 目标多一个 winfix 提交。
