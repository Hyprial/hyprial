# 打包宿主三层拆分（2026-11-13）

> **2026-11-14 已被取代：** 本文描述的原生壳（`clients/native/`）与打包线（`packaging/clients/`）已整体删除，
> 其安排由 `docs/design/gui-single-launch-2026-11-14.md` 取代。以下内容仅作历史记录保留，文中路径在当前树内不存在。

状态：**方案定稿，进入实施**。本文件定义新打包线的边界、构建接口和迁移步骤；它是
`build/packaging-host-20261113` 的决策记录，不是产品验收结论。

实施状态（2026-11-13）：

- P0 初版归属：`OWNERSHIP.tsv`（A=101 / B=75 / C=66，待分析结果细化）。
- P0.5 边界修订（Codex review 后）：新增 `A-BOUNDARY.md`/`A-BOUNDARY.json`，把 A 的允许、
  遗留冻结、必须迁出和禁止路径写成机器可读清单；主页面停止注入 `__hyprialAccount`，
  `main.json` 移除 `allow-account-invoke`；CI 边界门见
  `scripts/ci/check_packaging_host_boundary.py`。
- P1 已完成：A 层 native/打包已从 15c0b3707 干净移植到本分支（dev 在这两目录无改动），
  提交 `68ed00b43`；`cargo check/test --lib --features power-pack` = 510 passed / 2 个既有
  平台失败；结构门/模块边界门 PASS。
- P4 接口设计：见 `docs/design/packaging-host-split-20261113/TARGET-BUILD-INTERFACE.md`；
  执行细节（解析算法、清理、错误码）在 P0.5 后补入。
- P2 设计：`docs/design/packaging-host-split-20261113/SETUP-PAGE.md`（独立 setup 页面、native 接线、构建集成、风险）。
  setup 页是 **plain ES modules 新实现**，StartupWizard 只作行为参考，不迁移 React 组件。
- P3 分析：`docs/design/packaging-host-split-20261113/BACKEND-CONTRACT.md`。关键发现：2727 后端缺 A 宿主的 launch nonce/stdin-stop、promotion/initialize 生命周期与 harness CLI；正式 2727 包须先落 C（或 A 限期兼容补丁）。临时 demo 包（A 宿主 + 15c0b3707 后端 + 2727 GUI）不受影响。
  宿主契约版本化见 `docs/design/packaging-host-split-20261113/NATIVE-HOST-CONTRACT.md`。

## 1. 背景与目标

现有打包线（`build/packaging-unified-20261111` @ `15c0b3707`）把 native 壳、打包脚本、
安装检测界面、GUI 业务改动、后端业务改动和 CI 修复混在同一条 150+ 提交的线里。它完成了
首启、登录锁、promotion 等修复，但带来三个问题：

1. GUI 业务与打包线互相牵连：native 首启 seam（StartupWizard/product-bootstrap）长在
   `gui/` 里，打包线无法脱离某个具体 GUI 分支独立演进。
2. 新 GUI/业务线（如 PR #2727）无法直接被现有打包线消费：它缺少 native seam，而把整条
   打包线合进去会带来大量无关 GUI/后端历史。
3. 安装检测流程没有独立归属：它同时依赖 `gui/` 组件、native 桥和 Python CLI，导致“安装
   检测”既不算打包也不算业务。

目标：

- **新打包线独立**：只含 native 宿主、安装检测流程、打包脚本与宿主 CI；不含
  `hyprial/gui` 业务内容，也不分叉 `src/hyprial` 业务。
- **安装检测在业务之外**：成为 native 侧的独立 setup 页面，随打包线版本化。
- **任意业务分支可打包**：打包脚本接受目标分支/SHA，构建时消费目标的 GUI dist 与 wheel，
  自动带上安装检测流程。
- **账号/设备走 daemon-request 新路线**：业务 GUI 使用 connected onboarding 端口；打包线
  不向业务 GUI 注入 native account bridge。
- **可追溯**：provenance 同时记录宿主 SHA、目标分支 SHA、wheel/uv.lock 摘要与打包版本。

## 2. 目标架构：三层

| 层 | 归属 | 内容 | 不包含 |
|---|---|---|---|
| **A. 打包宿主线**（本分支） | packaging/native owner | native 宿主（窗口、boot、GUI host、setup/account 桥、product consent、promotion）、独立 setup 页面、安装检测编排、打包脚本（stage/bundle/verify/prepare-release）、宿主 CI 门 | `gui/**` 业务、`gui/src/client/generated`、`src/hyprial` 业务域 |
| **B. GUI/业务线**（如 PR #2727） | GUI/business owner | 页面、账号/设备 UI、Agent、平台、聊天；全部通过 daemon-request connected 端口取数 | StartupWizard/product-bootstrap、native capabilities、安装检测 |
| **C. 后端业务 + native 宿主契约** | backend owner | `src/hyprial` 业务域 + 原生宿主契约：`request_http` local-gui/bootstrap 配置、`BootstrapHttpHost`、`onboarding.status`/`auth.login.begin`/`onboarding.device.ensure`、catalog/phase、auth.logout admission、harness CLI | native 宿主 UI、打包脚本 |

### 职责矩阵（关键路径）

| 能力 | A | B | C |
|---|---|---|---|
| 窗口/导航/GUI host/launch grant | ✅ | | |
| 产品环境检测/Python 同意/托管安装 | ✅ | | |
| 独立 setup 页面（检测展示） | ✅ | | |
| harness 检测编排（native CLI 调用） | ✅ | | ✅ CLI 逻辑 |
| 产品 bootstrap 生命周期（boot_bootstrap/promotion） | ✅ | | |
| bootstrap request-http 服务与 onboarding 方法 | | | ✅ |
| 登录/设备 UI 与流程 | | ✅（connected） | |
| 业务页面/数据（daemon-request） | | ✅ | |
| native account bridge（遗留） | ✅（保留不注入） | | |

## 3. 关键设计决策

### 3.1 安装检测在业务之外（setup 页面）

- 主窗口初始加载 **packaging-owned setup 页面**（`ui/setup`），不加载业务 GUI；页面只做：
  Python 检测/同意/托管安装、harness 检测（Pi/Claude Code/Codex）展示、等待环境 ready。
- 环境 ready 后由 native 侧启动 runtime + GUI host，主窗口再导航到目标业务 GUI 的 managed
  page。业务 GUI 不需要知道安装检测存在。
- setup 页面的**行为参考**现有 `gui/src/features/product-bootstrap/impl/StartupWizard.tsx`，
  但不迁移 React 组件、不 import `gui/` 业务模块；按 `SETUP-PAGE.md` 用 plain ES modules
  在 `clients/native/setup-ui/` 新实现，保持文案与体验。`gui/**` 的 StartupWizard/
  product-bootstrap 文件归 B（2727 权威），A 不携带。
- 预运行时检测使用 Tauri IPC：`__hyprialSetup`（harness 清单）与
  `product_prepare_state`/`product_prepare_consent`。**这是 host-level 例外，不走
  daemon-request**：后端尚未启动，不能依赖它要启动的服务。

### 3.2 账号/设备走 daemon-request，旧 native 桥不注入

- B 线 Settings 使用 connected 端口（`auth.login.begin`/`onboarding.status`/
  `onboarding.device.ensure`），与 PR #2727 的实现一致。
- A 线**不向业务 GUI 注入 native account capabilities**：主窗口 `init_script` 不注入
  `window.__hyprialAccount`，`capabilities/power-pack/main.json` 不授予
  `allow-account-invoke`。`account_invoke`/旧 onboarding 窗口/`network-worker.py` 仅作为
  遗留冻结路径保留，不被业务 GUI 消费，后续单独删除。
- 边界由 `A-BOUNDARY.md`/`A-BOUNDARY.json` 和 CI 门
  `scripts/ci/check_packaging_host_boundary.py` 强制；不是仅靠约定。
- 这样不会覆盖 connected capabilities，也避免旧的 identity/daemon 锁冲突路径。

### 3.3 bootstrap 生命周期与 bootstrap 服务分离

- A 负责“谁启动/停止 ownerless bootstrap host、何时 promotion”。
- C 负责“bootstrap host 对外提供哪些 daemon-request 方法/catalog/phase”。
- 两者通过稳定契约连接：catalog 方法集合、phase、local-gui 配置 schema、启动参数，以及
  **`hostContractVersion` + features 能力位**（见 `NATIVE-HOST-CONTRACT.md`）。
  A 的 `gui_host/upstream.rs` 与 C 的 `request_http` 任何一方改契约都必须同步
  Contract-Impact；预检只靠方法名不够，必须校验启动证明、生命周期能力和 phase。

### 3.4 打包即组合（`--target`）

- 打包脚本增加显式目标：`--target <worktree|branch|sha>`。
- 打包线提供：native 宿主构建产物（二进制 + setup 资源 + 版本/摘要 manifest）、打包脚本、
  uv 二进制、签名/标识/安装器模板。
- 目标分支提供：`gui/` 源码与 `gui/dist` 构建、`src/hyprial` wheel、`uv.lock`。
- provenance 记录：host SHA、target SHA、wheel/uv.lock 摘要、打包版本、标识符。
- 打包前做契约预检：启动目标后端，验证 phase/catalog/方法可用性；不匹配则失败而不是
  静默出包。

## 4. 构建与产物接口

### 4.1 A 的宿主产物

- `clients/native/src-tauri` 构建出平台 app（Tauri），包含：
  - native host 二进制；
  - `ui/setup` 与 `ui/startup` 等内嵌资源；
  - 宿主版本、源码 SHA、README/manifest。
- 打包脚本消费该产物，不重新编译 GUI 业务。

### 4.2 目标分支输入

- `gui/dist`：由目标分支的 `npm --prefix gui run build` 产出；打包线校验 `index.html`
  与资源相对引用（沿用 `build-shared-ui/tauri-bundle` 的校验）。
- wheel：目标分支的 `src/hyprial` 构建；wheel 版本、源码 SHA 必须与目标分支一致。
- `uv.lock`：目标分支的锁；与 wheel 同源。uv 二进制由 A 提供。

### 4.3 输出与标识

- Mac DMG / Windows NSIS，test identifier 与正式 identifier 分离。
- provenance 文件与 SHA256SUMS 随包交付。
- 不发布、不移动 release/track/tag，除非另有明确授权。

## 5. 新打包线的目录与边界

```
clients/native/                 # A: 宿主
  src-tauri/                    #   窗口/boot/GUI host/桥/product/protection
  setup-ui/                     #   新增：安装检测 setup 页面（packaging-owned）
  native-cli/                   #   CLI 启动器
packaging/clients/              # A: 打包脚本、stage、verify、prepare-release
scripts/ci/                     # A: 宿主与打包 CI 门（不含 GUI 业务门）
```

禁止：

- 不在 A 分支修改 `gui/**` 业务；不复制 `gui/src/client/generated`。
- 不在 A 分支分叉 `src/hyprial` 业务；C 的契约改动由 backend PR 承载。
- 不把 native account bridge 注入 B；主窗口不注入 `__hyprialAccount`、不授予
  `allow-account-invoke`。
- 目标 GUI 只能经 `--target` staging 进入 `clients/native/src-tauri/ui/gui/`，不得提交进 A。
- 以上禁止项由 `A-BOUNDARY.json` + `scripts/ci/check_packaging_host_boundary.py` 检查。

## 6. 迁移步骤

### P0 盘点与分类

- 把现有线的 242 个改动文件/150+ 提交按 A/B/C 分类，产物为 `OWNERSHIP.tsv`（本目录附录）。
- 把分类变成 `A-BOUNDARY.json` 可执行清单；CI 门检查 changed paths、主页面 account 注入、
  setup-ui import 和 `ui/gui` 跟踪状态。
- 验收：每个改动文件有唯一归属；跨层依赖列出清单；门禁在 A 分支 PASS，在加入
  `gui/**`/`src/hyprial/**` 的 fixture 上 FAIL。

### P1 新线建立

- 从 `dev@7d4dc766` 建 `build/packaging-host-20261113`（本分支）。
- 迁入 A 的 native/打包子集（与 GUI 业务零重叠），保留首启、promotion、锁修复、setup 桥。
- 验收：`cargo test --lib`、宿主构建通过；无 `gui` 业务文件被带入。

### P2 setup 页面

- 新增 `clients/native/setup-ui`；按 `SETUP-PAGE.md` 用 plain ES modules 实现产品环境/同意/
  harness 检测能力，不迁移 React 组件、不 import `gui/`。
- 修改窗口初始页为 `ui/setup`；ready 后导航到业务 GUI managed page。
- 验收：fresh home 首启可完成 Python 同意/安装，显示 harness 检测结果；ready 后进入业务 GUI；
  业务 GUI 无需任何 native seam；setup-ui 的禁用 import/依赖检查 PASS。

### P3 后端契约（C）

- 明确 `request_http` local-gui/bootstrap 配置、catalog/phase、onboarding 方法为稳定契约。
- 现有线中的后端支撑改动按“契约”与“业务 bug fix”分类：前者入 C，后者随业务线；A 只保留
  临时兼容补丁（如有）并标注期限。
- 契约必须带 `hostContractVersion` + 启动/生命周期/能力位，见 `NATIVE-HOST-CONTRACT.md`；
  C1（硬契约）先落，C2a/C2b（平台/harness）按平台和旅程分批。
- 验收：目标分支（2727）后端与 A 宿主预检通过；无未申报的 contract 漂移；缺 nonce/
  initialize/feature 的 fake target 必须预检失败。

### P4 `--target` 打包接口

- 打包脚本支持目标 worktree/branch/SHA；构建目标 GUI dist + wheel + lock。
- 明确解析算法、浅克隆/脏树、后端来源、wheel/lock 一致性、失败清理和错误码（见
  `TARGET-BUILD-INTERFACE.md` 执行细节）。
- 加入预检（phase/catalog/方法/`hostContractVersion`/features）与双坐标 provenance。
- 验收：同一打包线可分别打出 2727 与任一 dev 分支的包；目标构建失败时不留半包、不改宿主
  checkout、不回退宿主 GUI/wheel。

### P5 CI/文档/测试

- A 的 CI：Rust 测试、setup 页面测试、打包 smoke（Mac/Windows）、结构门。
- 文档：本方案 + setup 页面契约 + 打包 `--target` 使用说明。
- 验收：A 线独立 CI 绿；不依赖 B 的 GUI 业务测试。

### P6 出 2727 包

- `--target origin/pr-2727` 构建；跑首启/登录/设备/promotion/聊天关键旅程。
- 验收：包与 provenance 交付；未测项明确 UNKNOWN/BLOCKED。

## 7. 验证矩阵

| 场景 | 验证 |
|---|---|
| fresh home 首启 | Python 检测/同意/安装、harness 检测、进入 |
| 已有环境 | 复用/ready、无需重复同意 |
| 登录/设备（B connected） | auth.login.begin → onboarding.status → device.ensure → promotion |
| 取消/失败 | setup 页面可重试；bootstrap host 恢复 |
| 业务页面 | daemon-request catalog/methodAccess；聊天历史（replyToMessageId） |
| 打包产物 | Mac DMG / Windows NSIS、identity、artifact digests |
| 目标分支切换 | 同一打包线打 2727 与 dev 分支 |

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| setup 页面迁移工作量大 | 先最小流程（Python 同意 + harness 检测），复用现有文案/样式；后续再扩展 |
| 目标 GUI 的 local-gui 契约不匹配 | 打包前预检 + 失败即停；必要时做一次很小的上游补丁，不放进 A |
| 后端契约归属不清 | P3 先定契约清单；A 的临时补丁单独文件 + 到期删除 |
| 2727 WIP 失败 | 包定位为评估包；只声明验证过的旅程 |
| 构建主机竞争 | worker 临时包与主线错峰；单重负载槽协调 |
| 旧 native account bridge 残留 | 不注入 B；后续单独删除，不作为新路线依赖 |

## 9. 工作分解与并行

- 主线（本分支）：P0→P5，按上面顺序。
- 并行分析 subagent：P0 分类、C 契约 diff、setup 页面依赖分析、`--target` 构建改造分析。
- 临时 2727 包：独立一次性分支，见
  `docs/notes/packaging-temp-2727-20261113/HANDOFF.md`；不阻塞主线。

## 10. 已确认与待确认

已确认（2026-11-13）：

1. C 的原生宿主契约由 backend 线落地（C1/C2 分批），A 仅保留限期补丁。
2. setup 页面不复制 `gui/` design primitives，只用自包含 CSS token 子集和 plain ES modules。
3. 旧 native account bridge 默认不注入；删除安排在首个正式包之后，单独 PR。
4. 2727 正式包先验收核心旅程（首启/登录/设备/聊天/退出），harness 检测随 C2b 分批。

待确认：

- 2727 同步 dev（含 C1）由 2727 owner 执行还是本线提供 cherry-pick；
- Windows 首个包是否与 macOS 同批（决定 C2a Windows 是否进入关键路径）。
