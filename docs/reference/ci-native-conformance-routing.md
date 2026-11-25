# Native conformance CI 选路与接手记录

## 结论与适用范围

本变更只调整 CI/workflow、共享选路及其测试和说明，不改产品源码，也不把文档 PR 一律免检。
`native-conformance` 复用 `scripts/ci/ci-plan.mjs` 的完整 merge-base 差异，新增内部 `native`
布尔输出。轻量 `plan` 作业先确认比较和 checkout 的精确 SHA，再决定是否启动既有 suite
作业；`native-summary` 汇总真实结果。未选择原生范围是 `NOT_SELECTED`，不是 conformance PASS。

2026-11-15 接手基线是 Forgejo `HyprialOS/hyprial` 的 dev
`182c118ab4030fbe40c2b9f43d8a4ea37836bbd1`，工作分支为
`ci/native-conformance-scope`，目标为 dev，合并后保留分支。最终候选 SHA、远端 run/job
证据和普通合入结果以本 PR 的 Forgejo 正文及检查页为准；不在文档里用一个无法自引用的
commit SHA 冒充最终 head。

## 已核对的问题证据

- 基线 `native-conformance.yml` 对所有目标为 dev 的 PR 触发，没有路径或共享 planner
  选路；`reference-abi`、`pykka`、三个原生平台作业和 `free-threaded-3-15t` 都会排队。
- `scripts/ci/ci-scope.mjs` 在该基线不把 `ROADMAP.md` 识别为文档，导致 shared/unknown
  全量计划，连带 GUI 和 Tailcat Go。
- PR #2818 是独立文档台账 PR，不是本选路 PR。其 head
  `5d1b62c5d4cf0930ff7ae5c71bf897465ea90a9c` 的 Forgejo task #47389 启动了
  `tailcat-go` 和 GUI，task #47390 启动了原生矩阵，直接说明无关工作被排队。
- 本次读取到 #47390 的 `reference-abi`、`pykka`、`native-linux-amd64`、
  `free-threaded-3-15t` 成功，以及 `native-darwin-arm64` 取消；PR 状态页另有后续/聚合
  失败状态。它们不是最新 dev 或连续完整 CI 的通过证据。历史某一精确 SHA 的全绿结果也
  只能约束该 SHA，不能被本变更借用。
- 未修改基线上的 `node --test --test-concurrency=2 scripts/ci/ci-scope.test.mjs
  scripts/ci/ci-plan.test.mjs` 为 **156 passed / 0 failed / 0 skipped**。新增回归后，
  旧实现先出现 4 个预期失败；实现后的结果和精确候选 SHA 记录于 PR 正文。

## 原生 ABI 输入边界

以下路径选择完整原生矩阵：

- `crates/`、`conformance/`、生成/暂存目录 `src/hyprial/_core/`；
- `src/hyprial/kernel/impl/actor_runtime/`，以及公共出口
  `src/hyprial/kernel/interface.py`、`src/hyprial/kernel/__init__.py`；
- `src/hyprial/shell/impl/packaging/native_engine.py`、
  `src/hyprial/gui_build_hook.py`；
- `pyproject.toml`、`uv.lock`（`crates/Cargo.lock` 已由 `crates/` 覆盖）；
- ABI 规范及其 clarifications、native build/distribution ADR 三份精确设计输入；普通设计文档
  不因标题包含 actor 就自动选择，避免把引擎路径规则扩张成文档目录豁免或全量触发；
- `scripts/build/build_native_engine.sh`、`manylinux_native_evidence.sh`、`build-wheel.sh`，
  以及 Rust/uv/CPython 准备和缓存脚本；
- `native-conformance.yml`、`gate-python.yml`、`kp027-native-candidate.yml`、
  `wheel-release.yml`。其它 workflow/policy 和共享 CI 输入按 fail-closed 规则也选择完整矩阵。

选择依据是实际 import/build 边界：`src/hyprial/kernel/impl/actor_runtime/backends/abi.py`
驱动 actor-runtime public port（reference-abi 或 native ABI 模块），原生后端加载
`hyprial._core.actors`，构建脚本编译 `crates/`
并暂存扩展。普通 daemon 业务代码、无关 `gui/` 和 `sidecar/` 不在这个闭包内，不选择矩阵。
新的未知路径、共享 CI/build 输入、workflow/policy 改动选择完整矩阵，避免 allowlist 漏判。

`ROADMAP.md` 是普通文档。`GLOSSARY.md`、
`docs/domains/squire/SKILL-template.md`、`docs/guides/cutover-runbook.md` 是测试直接读取的
输入：它们选择 Python 检查，不选择原生 conformance，也不进入 docs-only 快速通道。
`docs_only.py` 的精确测试输入表仍是 docs-only 判定的另一道保守边界，本变更没有放宽它。

手动 dispatch、schedule、main、release 和已批准的 deep-refactor 全量路线选择完整矩阵。
integration batch 的轻量路线不借故跳过引擎、共享或未知输入的原生检查。PR 比较失败、
checkout 与事件 head 不一致、无法验证的范围使 `plan` 失败；五个内容作业不启动，
`native-summary` 必须失败，不能输出 `NOT_SELECTED` 或 PASS。

## 其它重型 workflow 的审查决定

- `gate-python.yml`：保留现有公共 planner、pytest 分层、Tailcat Go component 选择和 `unit`
  汇总。`ROADMAP.md` 的文档识别修复消除 shared/unknown 误选；文档测试输入继续选择 Python。
- `gate-gui.yml`：保留 `gui-unit` 作业名和 planner，重步骤只随 `gui` 执行，
  `release-package` 只随 `package` 执行。不能只用 trigger paths，否则所需 context 可能消失。
- `mock-model-harness-e2e.yml`：已有 daemon/identity/CLI/contract runner/依赖锁及自身
  workflow 的 PR/push 路径过滤，手动入口保留，不需要本次另建选路。
- `kp027-native-candidate.yml`：只随 `kp027/**` push 或显式 source SHA dispatch，属于
  原生构建 canary；保留完整构建，不随普通文档 PR 触发。
- `wheel-release.yml`、`github-publish.yml`、`release-tag.yml`：保留 tag/显式 dispatch 和
  source SHA/发布保护，不能改成文档路径选路。
- `e2e-macos-isolated.yml`、`e2e-macos-credentialed.yml`：保留 schedule、版本 tag 和手动
  真实环境验证；其缺失或 BLOCKED 不是 PASS。
- `e2e-provider-calibration.yml`：保留版本 tag 上的精确依赖/fake-provider 路径和手动入口。
- `release-install-scripts.yml`：保留 dev 上安装器相关路径及显式 dev dispatch，不增加 PR 发布。
- `gate-pr-hygiene.yml`、`gate-pr-declarations.yml` 是轻量声明/卫生门，继续随 PR 运行，
  不用原生矩阵替代。

## arm64 lane 的事件规则（2026-11-15，owner 批准）

`native-linux-arm64` 与 `native-darwin-arm64` 只在 `plan` 输出 `native_arm64=true` 时运行。
选路仍在 `scripts/ci/ci-plan.mjs`，不在 workflow 里散落 `if:` 表达式：
`native_arm64 = native && (eventName != 'pull_request' || base 为 main 或 release/**)`。
`native-conformance.yml` 为此新增 `push: branches: [dev]`；dev push 的 `native` 仍由该 push 的
diff 决定，所以只有改到原生范围的 dev 落地才会排队 arm64。schedule 与 workflow_dispatch（本
workflow 目前只有后者）选择完整矩阵，arm64 随之运行。目标为 main / `release/**` 的 PR 保留
arm64（发版门需要合并前的证据）；目标为 dev、`intg/**`、`intg/batch-*` 的 PR 不运行。

汇总：arm64 被选中时要求四个阻断作业全部成功（`PASS mandatory=4/4`）；PR 上未选中时两个 arm64
作业必须为 `skipped`，输出 `NOT_RUN` 与 `verdict=PARTIAL_NO_ARM64 mandatory=2/4`，不是 `PASS`；
一旦它们在未选中时被执行、或在选中时被跳过，汇总红。

WIP PR（标题以区分大小写的 `WIP:` 开头）由 `makePlan` 给出 `wip=true`、`native=false`，五个 suite
作业全部 skipped，汇总输出 `verdict=NOT_RUN reason=wip-pull-request`；去掉前缀触发的 `edited`
事件按完整计划重算。规则、状态语义和在线核对项见 workflows README 的「WIP 拉取请求」一节。

## 汇总语义与 owner 决策

- 五个既有 suite job 名不变（P8 删除了 pykka job）。`native=false` 时，`native-summary`
  仅接受五个 `skipped`，并报告 `NOT_SELECTED`；任何意外执行都是失败。
- `native=true` 且 `native_arm64=true` 时，`reference-abi` 和三个原生平台作业必须成功；
  `native_arm64=false`（PR）时只要求 `reference-abi`、`native-linux-amd64`，见上节。每个平台仍执行
  三次真实 conformance，未删除测试、未新增 pytest skip/xfail、未弱化断言。
- `free-threaded-3-15t` 按既有 spec §14 D12 和 owner 决定继续非阻断；它必须实际到达
  success/failure 终态，取消、缺失或意外 skipped 仍使汇总失败。其 failure 在汇总中明确
  标为 non-blocking，绝不改写成 skipped。
- 本 PR 不修改分支保护。workflow 文件仍需授权 maintainer 普通合入；不得 force merge
  pending 检查，不删除源分支。

## 验证、风险和未闭合事项

候选工作树的结构检查、模块边界检查、共享 Node 测试和隔离 workflow 汇总测试均已执行；
首次锁定环境 pytest 因依赖下载在 900 秒上限处超时，不作为通过或失败，标准命令的重跑
结果与最终候选命令、计数、精确 SHA、远端结果见 PR 正文。主要风险是新增引擎依赖时 allowlist 漂移，
以及 Python/Node 两套文档分类规则不一致；未知/shared 输入 fail closed 是当前的补偿，
后续新增直接 ABI 输入时必须同步 `nativeExactInputs`/`nativeInput` 和回归矩阵。

- 真实远端原生矩阵、runner 可用性和本 PR 的连续 CI：创建 PR 前为 **UNKNOWN**，只接受
  Forgejo 上精确 head 的实际结果，不用旧 PR 结果替代。
- 服务器端 dev 必需检查配置：本次为 **BLOCKED**。`fj` 没有 branch-protection 读取命令，
  本机没有 `tea`，匿名 API 返回 403，浏览器扩展未连接。仓库文档声明的 required context
  已核对，但不能冒充服务器配置读数；未做任何分支保护写入。后续 maintainer 应只读确认
  `gate-python / unit*`、`gate-pr-hygiene / pr-hygiene*` 及原生相关 context 的现状，
  不需要也不应为本 PR 擅自新增保护项。
- `native-summary` 是新增 context；它是否成为 required 不由本 PR 决定。即使未设 required，
  `plan` 或汇总失败仍是真实 workflow 失败，不得称为通过。
- 五次连续完整 CI、产品功能、GUI/浏览器和真实 daemon 验收不在本 CI-only 变更的验证范围，
  均为 **UNKNOWN/NOT_RUN**，不得由选路测试推导。

## 只依赖 Forgejo 的接手步骤

1. 打开本 PR 的 Conversation、Checks、Files changed 和本文件，先记录 base/head 全 SHA、
   分支名与 run ID；不要使用旧 agent 的 workspace 或本地历史作证据。
2. 从内部 SSH 重新取得 dev 和 PR 分支：

   ```bash
   git clone --branch dev --single-branch ssh://git@git.internal.example.com/HyprialOS/hyprial.git hyprial
   cd hyprial
   git fetch origin dev ci/native-conformance-scope
   git switch -c ci/native-conformance-scope origin/ci/native-conformance-scope
   git rev-parse HEAD
   git diff --name-only origin/dev...HEAD
   ```

3. 将 HEAD 与 PR 正文声明的验证 SHA 比对；不同就以 Forgejo 最新 head 重新验证，不能拼接
   不同 SHA 的结果。 diff 应只含 CI/workflow、选路、测试和说明文档，不应出现 `gui/`、
   `src/hyprial/shell/impl/gui/`、`clients/`、`packaging/` 或产品源码改动。
4. 复跑焦点验证（Python 一律经 uv）：

   ```bash
   node --test --test-concurrency=2 scripts/ci/ci-scope.test.mjs scripts/ci/ci-plan.test.mjs
   uv run --locked --python 3.12 --extra test pytest -q \
     tests/tooling/ci/rustify/test_actor_rustify_ci.py \
     tests/tooling/ci/execution/test_ci_conditions.py \
     tests/tooling/ci/planning/test_ci_docs_only_classifier.py
   uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
   uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
   ```

5. 在 Checks 中区分 run、job、task：task outcome 判断是否失败，job status/needs 判断依赖
   是否运行，commit status 只是写入时快照。`skipped` 只在 `native=false` 时是预期选路，
   不是测试通过；比较失败和缺失日志不能当绿灯。
6. 普通 CI 全部达到所需终态后，由有权限的 maintainer 普通合入到 dev；不得 force merge
   pending 检查，不修改分支保护，保留 `ci/native-conformance-scope`。若 dev 已前进或发生
   冲突，从最新 dev 重新验证新 head，并把新 SHA/run 证据追加到 PR，不复用旧结果。
