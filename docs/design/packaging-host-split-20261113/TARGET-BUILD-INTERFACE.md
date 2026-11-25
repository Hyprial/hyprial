# `--target` 打包接口（P4 设计附录，2026-11-13）

本文件记录“打包线消费任意目标分支”的构建接口分析。状态：**设计稿，待实现**。主方案见
`docs/design/packaging-host-split-20261113.md`。

## 1. 当前构建假设

| 假设 | 证据 |
|---|---|
| GUI 永远从打包线自己的 checkout 构建 | `packaging/clients/scripts/build-shared-ui.cjs` 硬编码 `WT/gui` 与 `gui/dist`；`tauri-bundle.cjs` 无条件调用 `prepareSharedUi()`，把 `gui/` 构建进 `clients/native/src-tauri/ui/gui/` |
| wheel 从同一 checkout 构建 | `scripts/build/build-wheel.sh` 用 `git rev-parse --show-toplevel`，版本 `+g<short HEAD>`；`src/hyprial/gui_build_hook.py` 还会重建 `root/gui` 并在 `hyprial/_gui/release.json` 记录 `sourceCommit=HEAD` |
| 单坐标 provenance | `stage-wheel-runtime.cjs` 接受 `--source-commit`，但不校验 wheel 的 `+g<sha>` 与它一致；`build-release.cjs` 只写宿主侧的 `productProvenance.sourceCommit` |
| 接线门以宿主为基线 | `check-dmg-wiring.cjs` 读 `packaging/clients/release/build-source.json` 与 `WT/gui`，目标 SHA 会被拒 |
| 外部流水线单 HEAD | Mac `pipeline.sh` / Windows `scripts` 用单一 `COMMIT/TREE`/`expectedHead`（仓库外，具体检查 UNKNOWN） |

结论：**现有脚本无法直接打另一个分支**；需要显式目标解析、预构建 GUI dist 注入、双坐标
provenance 和契约预检。

## 2. 建议接口

```bash
node packaging/clients/scripts/build-release.cjs \
  --target <worktree|ref|sha> \
  --revision rN --output DIR \
  [--wheel FILE] [--uv FILE] [--target-ref REF] \
  [--host-commit SHA] [--allow-dirty-host] [--no-preflight]
```

- 目标解析：路径必须是干净的 git worktree；ref/SHA 用
  `git worktree add --detach` 在缓存目录物化。
- 记录：`targetCommit`、`targetTree=HEAD^{tree}`、`targetGuiTree=HEAD:gui`、`targetRemote`、
  `targetRef`；宿主记录 `hostCommit/hostTree`。
- 默认 fail-closed：宿主或目标 dirty 即拒绝；`--allow-dirty-host` 仅用于评估且 provenance
  标 dirty。
- 顺序：resolve/clean-check → 预检 → 构建目标 wheel（除非 `--wheel`）→ 运行时契约预检 →
  从目标 `gui/` 源码重建 `gui/dist` 并安装到 `clients/native/src-tauri/ui/gui` → 打包宿主 →
  验证。既有 `gui/dist` 不复用：干净 worktree 也可能带其他提交遗留的未跟踪 dist。
- uv 用打包线钉定的二进制（0.11.7）；`uv.lock` 用目标的。

### 2.1 执行算法与失败语义

**目标解析**

1. `--target <path>`：必须是 git worktree 根；`git -C <path> rev-parse --show-toplevel`
   等于自身；`git status --porcelain` 必须为空。
2. `--target <ref|sha>`：`git rev-parse <ref>^{commit}` 得到 `targetCommit`。对象缺失时
   返回 `TARGET_OBJECT_MISSING`，不静默 fetch、不猜分支；需要时由调用方显式
   `git fetch --depth=1 <trusted-remote> <sha>` 后重试。
3. 物化：`git worktree add --detach <cache>/<targetCommit> <targetCommit>`；再次验证
   HEAD、clean、`HEAD^{tree}`、`HEAD:gui` tree。
4. 记录 `targetCommit/targetTree/targetGuiTree/targetRemote/targetRef`；宿主另记
   `hostCommit/hostTree`。两组坐标不得互相满足。

**后端来源**

- wheel 必须从目标 worktree 的 `src/hyprial` 构建；`--wheel` 只允许显式提供，且必须校验
  `Version: ...+g<targetCommit 短 sha>`、`hyprial/_gui/release.json.sourceCommit` 等于
  `targetCommit`、`uv.lock` blob 等于 `git show <targetCommit>:uv.lock`。
- 禁止使用宿主 checkout 的 `src/hyprial`/`uv.lock`；宿主只提供 native 壳和打包脚本。

**目标预检**

- 目标 `uv.lock` 必须通过 `uv lock --check`；锁与 `pyproject.toml` 不一致时返回
  `TARGET_LOCK_MISMATCH`，不得先构建 wheel。
- 用钉定 uv 在隔离 HOME/runtime 中启动目标
  `python -m hyprial.shell.impl.request_http serve --launch-nonce-stdin`，经 stdin 送 nonce，
  读 `server.json` 与 `/daemon/request`，断言 `launchNonce`、`httpContractVersion`、
  `hostContractVersion`、`hostContract.launch`、`requiredPhases` 与 `requiredMethods` 可调用
  （`NATIVE-HOST-CONTRACT.md` §3）；失败返回 `CONTRACT_MISMATCH`。不触碰真实 daemon/home。
- `--no-preflight` 只跳过目标 GUI tree 与运行时契约检查；resolve、clean、wheel stamp 与
  `uv.lock` digest 检查始终执行。

**失败与清理**

- 构建在临时 payload/staging 目录进行；`ui/gui` 先写 staging，再原子替换。
- `finally` 清理：删除脚本创建的临时 worktree、staging、scratch；恢复宿主 `ui/gui`
  原状（没有则删除）；不留下半包。
- 任一阶段失败：写 `build-failure.json`（phase、exitCode、targetCommit、stderr 摘要），
  退出非 0；不回退宿主 GUI/wheel，不产出“部分成功”包。
- `--keep-worktree` 仅用于排障，provenance 标 `keptWorktree=true`。

**错误码**

| code | 含义 |
|---|---|
| `TARGET_UNRESOLVED` | ref/SHA 无法解析 |
| `TARGET_OBJECT_MISSING` | 浅仓库缺少目标对象 |
| `TARGET_DIRTY` | 目标 worktree 不干净 |
| `TARGET_NOT_WORKTREE` | path 不是 worktree 根 |
| `TARGET_WHEEL_MISMATCH` | wheel 版本/`release.json`/`uv.lock` 与目标不符 |
| `TARGET_GUI_INVALID` | 目标 `gui/dist` 缺 `index.html` 或引用不是相对路径 |
| `TARGET_LOCK_MISMATCH` | 目标 `uv.lock` 与 `pyproject.toml` 不一致（`uv lock --check` 失败） |
| `CONTRACT_MISMATCH` | `hostContractVersion`/features/phase/method 预检失败 |
| `BUILD_FAILED` | 目标或宿主构建失败；附 phase |

**CLI**

```bash
node packaging/clients/scripts/build-release.cjs \
  --target <worktree|ref|sha> --revision rN --output DIR \
  [--wheel FILE] [--uv FILE] [--target-ref REF] \
  [--host-commit SHA] [--allow-dirty-host] [--no-preflight] \
  [--dry-run] [--keep-worktree]
```

`--dry-run` 只解析目标、记录坐标、跑预检，不构建；用于 CI 和人工核对。

## 3. 文件级改动

| 文件 | 改动 |
|---|---|
| `build-shared-ui.cjs` | 参数化 `prepareSharedUi({guiDir, source, build, install})`，默认行为不变 |
| `tauri-bundle.cjs` | `bundle(payloadRoot, {ui})`；`ui.prebuilt` 时安装目标 dist，不重建 |
| `build-release.cjs` | 解析 `--target`；目标模式构建/注入；双坐标 provenance；保留现有位置参数模式 |
| `stage-wheel-runtime.cjs` | 可选 `--host-commit/--host-tree/--target-ref/--target-tree/--target-gui-tree`；`productProvenance` 增加 host/target 块；校验 wheel `+g<sha>` |
| 新 `resolve-target.cjs` | 目标解析、clean 检查、worktree 物化、SHA/tree 记录 |
| 新 `preflight-target-contract.cjs` | 隔离启动目标 `request_http`，断言 `server.json` 与 catalog 的 native-host 契约；不通过即 `CONTRACT_MISMATCH` |
| 新 `scripts/verify/preflight_product_target.py` | 用宿主 uv 安装目标 wheel，读 catalog/openapi，起隔离 daemon，断言 `onboarding.status`/`auth.login.begin`/`auth.operation.*`/`auth.relogin`/`onboarding.device.ensure` 与 phase；记录 catalog digest 与 `uv lock --check` |
| `verify-bundle.cjs` | 可选目标断言；host shell-tools 字节等价保持不变 |
| `check-dmg-wiring.cjs` | 拆宿主基线与目标基线（`--target`/`--build-source`）；目标 GUI/source 对目标查，host shell tools 对宿主查 |
| `packaging/clients/release/README.md` | 记录 `--target` 用法 |

## 4. Provenance

`productProvenance` 应包含：

- `sourceCommit` = 目标 wheel 的 SHA（供宿主 Rust 的 40-hex 校验）
- `host: {commit, tree, ref, dirty}`
- `target: {ref, commit, tree, guiTree, remote}`
- wheel 名称/版本/SHA；uv 版本/SHA；`uvLock` SHA；`guiDist` tree digest

断言：

- 目标 HEAD clean 且等于记录 SHA；
- wheel `+g<short>` 与目标 SHA 一致；
- `uv.lock` blob 等于 `git show <target>:uv.lock`；
- `hyprial/_gui/release.json.sourceCommit` 等于目标；
- host shell tools 未被目标覆盖。

**双坐标是附加信息，不放松宿主自身的 HEAD/tree/clean 检查。**

## 5. 流水线适配

- Mac/Windows 流水线的单一 `COMMIT/TREE`/`expectedHead` 改为 `HOST_*` 与 `TARGET_*` 两组，
  分别用 `git -C <root> rev-parse HEAD`/`HEAD^{tree}` 断言；任一缺失即失败。
- 宿主与目标用独立 workspace 步骤；目标的 SHA 不能用来满足宿主检查。
- 同样的拆分应用到四个单 SHA CI workflow（`wheel-release`、`release-main-dispatch`、
  `release-install-scripts`、`gate-gui` 的相关步骤）。

## 6. 最小实施顺序

1. `resolve-target.cjs` + CLI 解析 + 测试（默认宿主路径不变）
2. UI 注入（`build-shared-ui`、`tauri-bundle`）+ prebuilt-dist 测试
3. `stage-wheel-runtime` host/target 字段 + wheel stamp 校验 + 测试
4. `build-release --target`（先支持 `--wheel` + 目标 GUI dist）+ 双坐标 provenance
5. 自动构建目标 wheel + 预检；`check-dmg-wiring` 拆分；README
6. 验收：`--target origin/pr-2727`（33ec5390）；适配 Mac/Windows 脚本

## 7. 风险

| 风险 | 缓解 |
|---|---|
| 外部 Mac/Windows 流水线不在仓库，单 HEAD 检查未知 | 流水线改为双坐标；文档记录；不能在本仓库验证的部分标 UNKNOWN |
| 目标 worktree 的 clean/tree 与 wheel 版本严格性 | 用独立、锁定的目标 worktree；wheel 构建会临时改 VERSION 再还原 |
| 契约漂移：catalog 预检通过但登录/设备/promotion 仍失败 | 预检之外保留打包后的 smoke 门 |
| wheel hook 与宿主 GUI dist 两次构建可能不一致 | 安装 wheel 构建产出的 `gui/dist`，或记录两个 digest 但不强行相等 |
| `build-source.json`/`payload-lock.json` 仍是宿主基线 | 按目标/宿主拆分，避免拒绝合法目标或误标来源 |
| 宿主 `payloadVersion` 与目标 wheel 版本混淆 | provenance 分离记录，验收分别核对 |
