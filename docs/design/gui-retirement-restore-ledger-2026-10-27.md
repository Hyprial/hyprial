# GUI 退场与恢复台账（2026-10-27）

状态：实施台账，随实际清理 PR 入库；本文件不是恢复完成声明。
目标架构：新共享 UI 落 `gui/`，原生壳落 `clients/native/`，构建分发落
`packaging/clients/`；业务 CLI/daemon/agent/消息/Tailcat 保留在 `src/hyprial/`。
本批只做临时退场，不要求旧 GUI/desktop 打包可用，也不恢复旧 status/stop 或旧升级兼容。

- 清理前基线：`dev = 3252c112c2ddc29374ce04c3fda1463334d36f65`
  （`origin/dev`，2026-10-27 派单快照）
- 后端退场接线 PR：B #1998（`work/gui-retire-backend-20261027`，目标 `dev`；
  代码 head 为 `e8878600e499bc4891383e6a1db520ecaed93c8b`，其后仅文档提交
  （台账与活动入口说明），精确 PR head 以 Forgejo PR #1998 当前值为准；由独立审核
  合入，本 worker 不自合 dev）
- 纯 GUI/`app` 路径清理 PR：A #1999（`work/gui-retire-paths-20261027`，head
  `d9b95380589e14dee4ef024a7a87f913ed07b27b`）→ 干净
  `intg/gui-retire-20261027`（= dev `3252c112`，不继承旧 `intg`）
- 公共 CI 配套：C #1997，`local-windows-worker`
  （request `workflow-request:a9808ce782cc47138c99b8a10e5e5102`，head
  `d2a662bc84825f19aac67da06963224320fd281c`，目标 `dev`）
- 合入顺序：C 公共 CI 配套 → B 本后端接线 PR → A 纯 GUI 路径清理到
  `intg/gui-retire-20261027` → A 提升到 `dev`（独立审核）。所有发布 PR 本 worker 不代合。
- 依赖说明：B 的 `src/hyprial/shell/impl/gui/**` 路径被 `ci-plan.mjs` 归为 GUI，
  普通 head 直指 `dev` 会触发 `CI_ROUTE`；C #1997 明确不改 planner，该实际路由待
  operator 决定。本 worker 不修改 `scripts/ci` 或 `.forgejo`。A 删除路径后还需要 C
  更新结构根和 GUI npm 前置。

## 字段与状态约定

每个入口记录：精确旧路径/符号、清理前 SHA、本次改动/PR、原因与保留消费者、
新位置/恢复 owner 或 PR、验证命令/结果、状态。

状态只用：`保留待恢复`、`已清理待验证`、`已清理并轻量验证`、`暂留`、
`恢复候选`、`恢复验证完成`。提交/PR 存在不等于恢复完成；未运行项写 `NOT_RUN`，
skip 不冒充通过。

## R01 — 构建钩子注册

- 旧入口：`pyproject.toml` `[tool.hatch.build.hooks.custom]`
  `path = "src/hyprial/gui_build_hook.py"`；`scripts/ci/check_module_boundaries.py`
  的 `OUTER_ENTRIES = {"cli.py", "gui_build_hook.py"}`。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动：`pyproject.toml` 注册保留；钩子内部不再加载/构建 GUI（见 R02）。
  `scripts/ci/check_module_boundaries.py` 本批不改（CI owner）。
- 原因/保留消费者：CLI wheel/sdist、worker image、发布构建仍需要这个薄钩子承载
  Tailcat 载荷；删除注册会破坏 CLI 安装链。
- 新位置/恢复 owner/PR：新 GUI 资源构建接入由后续 GUI/build PR 设计，不复用旧 hook
  硬编码；恢复 owner 待 coordinator 指定。当前 CI 结构门仍把 `gui_build_hook.py` 当
  外壳入口，后续改根配置时由 CI owner 同步。
- 验证：导入/钩子静态测试见 R02/V5；`uv build --wheel` 重负载 `NOT_RUN`（需 slot）。
- 状态：`保留待恢复`。

## R02 — `gui_build_hook.py` GUI staging、worker-image provenance

- 旧入口/符号：`src/hyprial/gui_build_hook.py` 模块级
  `bundle_tools = runpy.run_path(...gui/bundle.py)`、`build_gui`、
  `CustomBuildHook._stage_gui`；`scripts/build/docker/build-worker-image.sh`
  `stamp=src/hyprial/gui-source.json`、`git status -- src/gui`、`runpy ... bundle.py`
  及 stamp trap；`scripts/build/docker/Dockerfile.hyprial-worker` 旧 GUI 注释；
  `src/hyprial/shell/impl/packaging/tailcat_wheel.py` 旧 `bundle.py` 注释。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动（本 PR）：`gui_build_hook.py` 只加载 `tailcat_wheel.py`，删除 GUI 导入和
  `_stage_gui`，保留 `HYPRIAL_BUILD_WORKER_WHEEL=1` 与
  `HYPRIAL_TAILCAT_ALLOW_MISSING` 语义；worker-image 脚本删除 GUI provenance
  stamp/git 探针，直接 `uv build --wheel` → 版本读取 → `docker build`；Dockerfile 和
  tailcat 注释去掉旧 GUI provenance 声明。
- 原因/保留消费者：旧 GUI 构建与 `src/gui` 同时退场，但 CLI wheel/sdist 和 worker
  wheel 的 Tailcat 载荷必须继续可构建；worker wheel 仍由 builder stage 提供 sidecar。
- 新位置/恢复 owner/PR：新 GUI 静态资源构建/载荷契约由后续 GUI + packaging PR 恢复；
  新位置按 `gui/`、`packaging/clients/`；owner 待 coordinator 指定。
- 验证：`tests/shell/packaging/test_gui_build_hook_worker_switch.py`（含“无 GUI stage”
  断言）、`tests/shell/packaging/test_tailcat_wheel.py`、
  `tests/tooling/containers/test_build_worker_image.py`、
  `tests/tooling/containers/test_worker_image_gitless_wheel.py` 静态臂。真实 wheel 构建
  属 V7，`NOT_RUN`，需先报告资源/slot。
- 状态：`已清理并轻量验证`（真实 wheel/docker 见 V7 `NOT_RUN`）。

## R03 — 旧 GUI 构建/解包模块与 `_gui` 载荷

- 旧入口/符号：`src/hyprial/shell/impl/gui/bundle.py`（`REQUIRED_INPUTS`、
  `build_gui`、`stamp_snapshot`、`source_revision`）、
  `product.py`（`product_bundle`/`PRODUCT_INPUTS`）、`unpack.py`
  （`ensure_unpacked`、`unpacked_state`、`gui_source`）；
  `src/hyprial/shell/interface.py` 的 `product_gui_bundle` 三处注册；
  wheel 内 `hyprial/_gui` 载荷与 `src/hyprial/gui-source.json`。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动（本 PR）：删除三个实现文件；从 `shell/interface.py` 删除
  `product_gui_bundle` 的 TYPE_CHECKING 导入、`_FACADE_EXPORTS` 和 `__all__` 注册。
  其余 GUI facade 名称保留并指向 fail-closed 占位（R04）。不删除用户数据。
- 原因/保留消费者：旧资源定位/解包与新架构无关；保留消费者是通用 CLI 构建（R01/R02）。
- 新位置/恢复 owner/PR：新 GUI 资源定位、完整性、版本兼容与升级由后续 GUI/packaging
  PR 定义；恢复 owner 待 coordinator 指定。
- 验证：删除后无 `product_gui_bundle`/`bundle.py`/`product.py`/`unpack.py` 活动导入
  （静态检索）；CLI 导入和焦点测试见 V1/V2。真实 wheel 载荷 `NOT_RUN`。
- 状态：`已清理并轻量验证`。
## R04 — `hyprial gui` 注册、apps/runtime、facade

- 旧入口/符号：`src/hyprial/shell/impl/gui/apps.py` 旧生命周期/选择器/upgrade；
  `runtime.py` 的 `gui_status`/`start_gui_background`/`stop_gui`、`_lifecycle_lock`、
  `_READY_TIMEOUT` 等；CLI `commands/gui/commands.py` 与
  `commands/upgrade/commands.py` 的旧文案；`shell/interface.py` facade 名称；
  `tests/supervision_exemptions.json` 的 GUI `_READY_TIMEOUT` 豁免。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动（本 PR）：`apps.py` 与 `runtime.py` 改成最小 fail-closed 占位，保留公共
  文件名、函数名、签名和 `hyprial gui [start|status|stop|upgrade]` 语法；所有操作抛
  `GUI_UNAVAILABLE`，不创建 `$HYPRIAL_HOME/apps/`，不恢复旧 status/stop。CLI help
  文案改为“临时不可用”。`_unpack_paired_gui` 捕获错误并记录
  `{"ok": false, "code": "GUI_UNAVAILABLE"}`，不让已成功的 daemon upgrade 失败。
  删除 supervision 清单中的 `_READY_TIMEOUT` 条目并同步
  `tests/tooling/architecture/migration/test_authority_migration.py` 的 68 条摘要。
- 原因/保留消费者：CLI 命令注册、`entry.py`、`services.py`、`shell/interface.py` 和
  升级路径仍导入这些公共名称；删除名称会造成悬空 import。保留是为了明确失败而非假装成功。
- 新位置/恢复 owner/PR：新 Web start/status/stop 和明确的 GUI upgrade 语义由新 GUI
  后端适配 PR 恢复，不照搬旧进程管理；owner 为后端 owner（待 coordinator 指定）。
- 验证：`test_gui_apps.py`、`test_gui_runtime.py`（含 no-home-mutation 和 stable code）、
  `test_upgrade_gui_unpack.py`（GUI 不可用不阻挡 daemon upgrade）、CLI import/help 焦点
  测试；见 V1/V2。旧生命周期/真实 GUI NOT_RUN。
- 状态：`已清理并轻量验证`。
## R05 — 旧 `src/gui/**` UI、exports、启动脚本

- 旧入口：`src/gui/**` 全部跟踪文件（`client/`、`core/`、`host/`、`plugins/`、
  `presentation/`、`product/`、`scripts/`、`session/`、`studio/`、`transport/`、
  `tests/`、`legal/` 等）。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动：PR A #1999（head `d9b953805`）删除 `src/gui/**` 共 78 个跟踪文件；
  本后端 PR 不删除该树。PR A 为纯删除，0 新增/修改。
- 原因/保留消费者：旧 GUI 临时退场；旧 GUI CI 文件保留但预期红，登记 R11。不得在
  删除后恢复旧页面兼容。
- 新位置/恢复 owner/PR：新共享组件 UI 落 `gui/src`；恢复由组件化 GUI 主线按
  ADR-GUI-004/006 独立 PR 进行，owner 由 coordinator 指定。
- 验证：PR A #1999 删除后 `git grep` 无活动运行消费者；仅 CI/历史证据引用，登记
  R11/R12。新 GUI 组件验收另案 `NOT_RUN`。
- 状态：`已清理并轻量验证（PR A #1999）`。

## R06 — 旧 GUI 开发预览入口

- 旧入口：`src/gui` 内开发服务器/启动脚本和旧 `hyprial gui` 预览路径。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动：随 R05 的 PR A #1999 同批删除 `src/gui/scripts/start-gui.sh`、开发预览/
  product server 源与测试；新 dev-mode 未实现。
- 原因/保留消费者：旧预览入口不再活动；不恢复旧 status/stop。
- 新位置/恢复 owner/PR：`gui` 开发服务器与独立 CLI dev-mode 接线，后续独立 PR；
  owner 由 coordinator 指定。
- 验证：旧预览入口在 PR A #1999 后不可用；新入口热更新、路径/端口、退出隔离仍
  `NOT_RUN`。
- 状态：`已清理并轻量验证（PR A #1999）`。

## R07 — 旧 `app/**` 原生壳、Tauri/Android/iOS、mobile UI

- 旧入口：`app/**`（`src-tauri/`、`src/`、`components/dsh`、移动 UI、旧 app 预检脚本）
  与 `tests/integration/desktop/test_tauri_handoff.py`。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动：PR A #1999（head `d9b953805`）删除 `app/**` 共 262 个文件，并删除
  `tests/integration/desktop/test_tauri_handoff.py`、仅服务旧 app 构建的
  `scripts/verify/app-env-preflight.{mjs,test.mjs}` 与
  `docs/guides/app-environment-preflight.md`；本后端 PR 不删除。
- 原因/保留消费者：旧桌面/移动客户端实现临时退场；核心 harness 保留在 `src/hyprial`。
  旧 `desktop-check`/`desktop-release` CI 文件暂不改，登记 R11。
- 新位置/恢复 owner/PR：`clients/native/`，接 `gui` 共享产物和平台适配；五端验证不得
  用网页预览替代；owner 由 coordinator 指定。
- 验证：PR A #1999 后旧 app 命令/文件不可用；`app-env-preflight` 无剩余活动引用。
  新原生壳 `NOT_RUN`。
- 状态：`已清理并轻量验证（PR A #1999）`。

## R08 — 旧 app 构建/stage/平台脚本、运行时组件

- 旧入口：`app` 构建/stage/平台脚本、运行时载荷清单和旧 app 环境文档。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动：随 R07 的 PR A #1999 删除 `app/scripts/**`、`app/src/runtime/**`、
  组件/payload 清单等构建与 stage 源；本后端 PR 不删除。
- 原因/保留消费者：不恢复旧打包兼容；新桌面要求自包含、与外部 CLI 隔离（ADR-GUI-003/005）。
- 新位置/恢复 owner/PR：`packaging/clients/`，构建组包/载荷清单/签名模板/下载页；
  owner 由 coordinator 指定。
- 验证：PR A #1999 后旧路径不可用；新干净环境载荷验证 `NOT_RUN`。
- 状态：`已清理并轻量验证（PR A #1999）`。

## R09 — 旧 app 发布/签名/安装/更新清单

- 旧入口：`app` 发布/签名/安装/更新清单、旧 app release 脚本。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动：代码随 PR A #1999 删除（`app/release/**`、`app/signing/**`、更新清单/脚本）；
  不撤销、不读取真实签名凭据，不改发布/OTA 权限。
- 原因/保留消费者：旧发布路径退场；在线包和用户数据保留，删除仓库源不等于停服务。
- 新位置/恢复 owner/PR：新分发/升级按 `packaging/clients/` 和平台更新契约恢复；发布
  签名操作仍不在本任务授权内。
- 验证：PR A #1999 后旧发布路径不可用；安装/A→B/回滚 `NOT_RUN`。
- 状态：`已清理并轻量验证（PR A #1999）`。

## R10 — 旧 app 下载页/feedback/上传发布

- 旧入口：`app/download-site`、`app/feedback`、旧上传/发布入口。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动：随 PR A #1999 删除仓库旧源（`app/download-site/**`、`app/feedback/**`）；
  不停止、不改动在线下载/反馈服务。
- 原因/保留消费者：在线服务归属和用户下载包不在本清理范围。
- 新位置/恢复 owner/PR：新分发产物与内网下载页接线由 packaging/分发 owner 另案；
  不能把删除源写成停服。
- 验证：仓库源删除已由 PR A #1999 轻量核对；新页面/包路径/hash/head 后续验证
  `NOT_RUN`。
- 状态：`已清理并轻量验证（PR A #1999；在线服务不动）`。

## R11 — GUI 专属 CI、planner/scope、结构根

- 旧入口/符号（本批不改，登记悬空）：
  `scripts/ci/ci-plan.mjs` 的 `component()` / `guiBranch` / `CI_ROUTE`；
  `scripts/ci/check_structural_limits.py` 的 `PRODUCT_ROOTS`（`src/gui`、`app/src`）、
  `EXCLUDED_PATHS`；`scripts/ci/check_module_boundaries.py` 的 `OUTER_ENTRIES`；
  `.forgejo/workflows/tests.yml` 的无条件 `npm --prefix src/gui ci` 与 GUI 条件步骤；
  `.forgejo/workflows/gui.yml`、`desktop-check.yml`、`desktop-release.yml`；
  `scripts/ci/pytest_impact.py` 的 GUI 映射和“删除=full”规则；
  `scripts/ci/pytest-impact-map.json`、`pytest-durations.json`；
  `docs/reference/ci-integration-routing.md` 中 GUI ownership/旧路径描述；
  CI-policy tests 中旧 GUI/desktop 路径断言。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动：**本 worker 不修改 `scripts/ci` 或 `.forgejo`**。已向 operator/CI owner
  报告精确路由矛盾：B 的 `src/hyprial/shell/impl/gui/**` 被归为 GUI，普通 head 直指
  `dev` 会 `CI_ROUTE`；A 删除 `src/gui`/`app/src` 会触发 `missing-product-root` 和
  unconditional npm 失败。C 公共 CI 配套为本批依赖。
- C 范围（`local-windows-worker` 2026-10-27 声明）：仅
  `scripts/ci/check_structural_limits.py`、四个 CI-policy 测试、`.forgejo/workflows/tests.yml`
  两处旧 GUI npm 前置；**不改** `gui.yml`/`desktop-check.yml`/`desktop-release.yml`。
  结构 gate 新语义为产品根存在则扫描、缺失不再报 `missing-product-root`。因此 A 合前
  必须 rebase 到含 C 的 dev；但 `gui.yml`/`desktop-*` 在 A 删除后仍是悬空红项，需后续
  CI owner 处理，不能写成“C 已清理全部 GUI CI”。`docs/reference/ci-integration-routing.md`
  的旧 GUI ownership 描述不由本 worker 改写，随 planner 规则交 CI owner 同步，避免双写。
- C PR #1997（head `d2a662bc84825f19aac67da06963224320fd281c`，base dev
  `3252c112`）已提交，尚未合入。其描述称恢复台账仍为本地准备，与事实不符：台账已随
  本 PR #1998 提交至 `docs/design/gui-retirement-restore-ledger-2026-10-27.md`；已向
  C owner 同步，合入后以实际 dev SHA 为准 rebase A。
- **B 落地路由阻塞（2026-10-27 实测）**：B #1998 目标 dev，改动含
  `src/hyprial/shell/impl/gui/**`；`scripts/ci/ci-plan.mjs` 的 `component()` 将该前缀
  归为 `gui`，PR 路由仅接受 head/非 dev base 的 `intg/gui*`，否则 `CI_ROUTE`。
  C #1997 明确不改 planner、不加例外，因此 C 合入后 B→dev 仍会被拒。A #1999 目标
  干净的 `intg/gui-retire-20261027`，路由可通过。已报 operator 选择：B 改目标干净
  intg、为 B 新建授权的 `intg/gui-retire-backend-*` head、或 CI owner 加仅限本次
  过渡的 planner 例外；本 worker 不自行改 planner/不伪造分支。B 当前不能声称可合入。
- 原因/保留消费者：未经授权的 CI 改动会扩大范围；把 GUI-only 红写成通用绿是错误。
- 新位置/恢复 owner/PR：CI owner + `local-windows-worker` 的 C PR；后续独立 CI PR 对接
  `gui/`、`clients/`、`packaging/` 并重新核 required contexts。实际 required 值本批
  `UNKNOWN`（历史证据：`tests / unit*`、`tests / cli-help-inventory*`、
  `skill-declaration / Skill-Impact*`）。
- 验证：本批仅静态登记；`fj pr status 1998` 于 2026-10-27T06:38Z 显示
  `gui / gui-unit` Failure、`tests / unit` Pending，且 `tests / checks` 尚未完成。
  未取得运行日志，不能把该红项单独归因；本地同一 head 的结构门与模块边界门通过，
  `CI_ROUTE` 由 `ci-plan.mjs` 静态探针单独证明。A+C 依赖干跑：以 C 分支的
  `check_structural_limits.py` 对 A 删除树运行得到 PASS（3464 files、945 modules、
  `violations: []`）；A 的 `base-freshness`/`submitted-by` 本地门也通过。该干跑不替代
  C 合入后的 A rebase 与实际 CI。实际 required contexts 仍 `UNKNOWN`，不声称绿色。
- 状态：`暂留`（owner 待 C/CI owner 处理；B 路由待 operator 选定）。

## R12 — 通用 pytest/build-worker/Docker/release 中的 GUI 消费者

- 旧入口/符号：
  `scripts/build/docker/build-worker-image.sh` 的 GUI provenance（已删）；
  `scripts/build/docker/Dockerfile.hyprial-worker` 旧 GUI 注释（已改）；
  `.dockerignore` 的 `src/gui/node_modules`（已删）；
  `scripts/ci/github_publish.py` 的条件
  `out/src/hyprial/shell/impl/gui/bundle.py` + `src/gui/package.json` provenance 块
  （保留但条件已恒假，登记给 CI owner）；
  `tests/tooling/release/publish/github/test_snapshot.py` 的 GUI provenance 用例（已删）；
  `tests/support/release/gui_release_test_support.py`（已删）；
  `scripts/ci/check_hyprial_merge_residuals.py` 相关测试（保留，合成 fixture）；
  `scripts/refactor/integration-contract.python.json` 与
  `scripts/refactor/PENDING-DECISIONS.md` 仍描述旧 `src/gui`/`gui_runtime` 模块
  （历史重构输入，无活动执行消费者，保留不改）。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动：worker-image 最小 GUI stamp 清除和对应测试改写在本 PR；发布 snapshot
  GUI provenance 测试删除；`github_publish.py` 不改，留作悬空 CI 条目给 CI owner。
- 原因/保留消费者：CLI 发布和 worker image 构建仍需工作；不能恢复旧 GUI 硬编码。
- 新位置/恢复 owner/PR：新载荷契约与发布构建由 packaging/release owner 后续 PR；
  `github_publish.py` 死块由 CI owner 删除或改写。
- 验证：worker-image 静态测试、snapshot 测试见 V5；真实 docker/wheel `NOT_RUN`。
- 状态：`已清理并轻量验证`（CI 死块 `暂留`）。

## R13 — 旧 GUI 测试、fixture、结构根、保留 adapter 测试

- 旧入口/文件（本 PR 删除）：
  `tests/shell/gui/ci/__init__.py`、`test_gui_ci_migration.py`；
  `tests/shell/gui/packaging/__init__.py`、`test_gui_product.py`、
  `test_gui_release_package.py`、`test_gui_snapshot_modes.py`；
  `tests/shell/gui/lifecycle/test_gui_pac.py`、`test_gui_profile_recovery.py`、
  `test_gui_unpack.py`；`tests/support/release/gui_release_test_support.py`。
- 改写保留：`tests/shell/gui/lifecycle/test_gui_apps.py`、`test_gui_runtime.py`
  改为 fail-closed 覆盖；`tests/shell/upgrades/test_upgrade_gui_unpack.py` 改为
  GUI 不可用不阻挡 upgrade；`tests/shell/packaging/test_gui_build_hook_worker_switch.py`
  增加“无 GUI stage”；`test_tailcat_wheel.py` 移除 `_gui` fixture 依赖；
  `test_build_worker_image.py`、`test_worker_image_gitless_wheel.py` 移除 GUI
  stamp/fixture；`test_snapshot.py` 删除 GUI provenance 用例；
  `test_open_source_project_docs.py` 更新文档断言；
  `tests/supervision_exemptions.json` 与 `test_authority_migration.py` 移除 GUI
  监管豁免（68 条）。
- 保留：`tests/shell/gui/adapter/**` 全部 adapter/移动/授权/会话测试（R15），以及
  `tests/tooling/ci/tailcat_wheel/test_verify.py` 中合成 `_gui` wheel 断言（不再对应
  实际构建载荷，待新载荷契约更新；本批不改 CI 轮子契约测试）。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 原因/保留消费者：删除旧实现专属测试不能整批屏蔽通用测试；保留 adapter 覆盖；
  新 UI/服务/打包测试由恢复 PR 补齐。
- 新位置/恢复 owner/PR：新 GUI 测试在 `gui/`/`clients/native/`/`packaging/clients/`
  对应 PR；恢复 owner 由各线 owner 承担。
- 验证：焦点测试 V1/V2/V5/V6；全量 pytest 禁止（4GB WSL），`NOT_RUN`。
- 状态：`已清理并轻量验证`。
## R14 — README/AGENTS/skills/架构与开发指引

- 旧入口/文件：`README.md` Development/Contributing、`CONTRIBUTING.md` 开发环境、
  `AGENTS.md` 环境/布局/Skills、`src/hyprial/skills/hyprial-ops/SKILL.md` GUI 工作空间、
  `.agents/skills/gui-workflow/SKILL.md` 活动 GUI 指引；README/AGENTS 链接的
  `docs/design/design.md` 架构入口与 `docs/reference/cli-top-level-command-consolidation.md`
  命令盘点仍把旧 `src/gui`/desktop 和 `gui` 生命周期写成当前能力。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动（本 PR）：活动入口改成“临时退场 / `GUI_UNAVAILABLE`”，删除 npm/GUI 构建
  命令和 Node/Rust fixture 断言；GUI skill 顶部明确标历史且不可执行；保留历史技术内容
  但不代表本版能力；`docs/design/design.md` 加退场说明并标明旧目录布局为历史；
  `docs/reference/cli-top-level-command-consolidation.md` 加 2026-10-27 状态并对 `gui`
  行改为失败封闭语义；新增本台账入口。
- 原因/保留消费者：新成员/agent 不能误以为旧 GUI 可运行；历史证据保留但标记。
- 新位置/恢复 owner/PR：新目录/命令由 `gui/`、`clients/native/`、`packaging/clients/`
  恢复 PR 更新；owner 为各线 + docs owner。
- 验证：`test_open_source_project_docs.py` 焦点断言；文档链接存在性静态检查。
- 状态：`已清理并轻量验证`。
## R15 — 共享 adapter/授权/会话/移动服务

- 旧入口/保留：`src/hyprial/shell/impl/gui/adapter/**`（25 文件，含 mobile pairing、
  auth envelope、host exposure、chat adapter 等），对应
  `tests/shell/gui/adapter/**` 测试全部保留；`contract/sidecar-tailcat/README.md`
  相关引用不变。
- 清理前 SHA：`3252c112c2ddc29374ce04c3fda1463334d36f65`。
- 实际改动：本 PR 不删除、不改语义；目录名不是删除理由。仅更新 `adapter/host.py`
  和 `adapter/chat.py` 顶部文档中指向旧 `app/src-tauri`、`src/gui/core` 的路径说明，
  代码、API、认证和权限不变。
- 原因/保留消费者：公共授权、会话、移动接入仍由新 GUI 后端消费者使用；删除会造成
  真实功能损失。未回退 master-key/认证契约。
- 新位置/恢复 owner/PR：新 GUI 后端适配由后端 owner 逐项独立 PR，避免双实现；恢复
  owner 待 coordinator 指定。
- 验证：`tests/shell/gui/adapter/**` 焦点测试 PASS（见 V4）；真实授权/收发 `NOT_RUN`。
- 状态：`保留待恢复`（本批保留）。

## 补充入口索引（相对 R01–R15）

| R | 旧入口/能力 | 本批实际处理 |
| --- | --- | --- |
| R01 | `scripts/ci/check_module_boundaries.py` `OUTER_ENTRIES` | 保留；CI owner 后续改根时同步 |
| R02 | `build-worker-image.sh` GUI stamp；`Dockerfile.hyprial-worker` 注释；`tailcat_wheel.py` 注释 | 已清除/更新；worker wheel 与 Tailcat 语义保留 |
| R03 | `shell/interface.py` `product_gui_bundle`；`hyprial/_gui` | 已删除注册与旧载荷生产；用户数据不动 |
| R04 | `entry.py`/`services.py` 的 `start_gui_background`；`supervision_exemptions.json` GUI 常量 | 公共名称保留并 fail-closed；监督豁免删除并更新摘要 |
| R05/R06 | `src/gui` 开发预览/启动脚本 | PR A #1999 已删除 78 文件；新 dev-mode 另案 |
| R07 | `app/components/dsh`、旧 app 预检、`tests/integration/desktop/test_tauri_handoff.py` | PR A #1999 已删除；其他 desktop network/SDK 测试保留 |
| R08 | `app` 构建/stage/运行时载荷 | PR A #1999 已删除；新 packaging 另案 |
| R09 | `app` release/签名/安装/更新清单 | PR A #1999 已删除代码；凭据/服务不动 |
| R10 | `app/download-site`、feedback、上传发布 | PR A #1999 已删除源；在线服务不动 |
| R11 | `pr-hygiene` 无 path filter 的结构门；`tests.yml` npm 前置；`gui.yml`/desktop workflows；`ci-plan.mjs` GUI 路由 | 本批不改；C PR #1997 仅覆盖结构门/CI-policy/`tests.yml` 两处 npm；B→dev 仍被 `CI_ROUTE` 拒绝（待 operator 选路）；`gui.yml`/desktop 仍悬空；required 实际值 UNKNOWN |
| R12 | `github_publish.py` GUI provenance 条件块；`.dockerignore`；发布 snapshot 测试 | `.dockerignore`/测试已改；publish 死块登记 CI owner |
| R12 | `scripts/refactor/integration-contract.python.json`、`PENDING-DECISIONS.md` 旧 GUI 模块/读写集描述 | 历史重构输入，无活动消费者；保留不改，新 GUI 迁移时复核 |
| R13 | `tests/tooling/ci/tailcat_wheel/test_verify.py` 合成 `_gui` 断言；结构根测试 | 保留合成断言（暂留，待新载荷契约）；焦点测试按 V1–V6 |
| R14 | `CONTRIBUTING.md` npm 行；`README` Tauri/cargo；`AGENTS` GUI 命令；`docs/design/design.md` 旧 GUI/desktop 布局；`docs/reference/cli-top-level-command-consolidation.md` `gui` 行；`docs/guides/app-environment-preflight.md` | 文档入口已更新为退场说明；架构入口标历史并指向本台账；CLI 盘点加 2026-10-27 失败封闭状态；app 预检指南由 PR A #1999 删除 |
| R15 | adapter 测试与 `contract/sidecar-tailcat/README.md` 引用 | 全部保留，不改语义 |

## 验证记录（按当前工作树；未运行项明确 NOT_RUN；2026-10-27T06:22–06:56Z）

下表 head 均为“测量时的精确 head”。本表自身更新会再产生文档提交；代码与
registry 不变，精确 PR head 以 Forgejo 为准，合并时 CI 在最终 head 重跑。

| ID | 范围 | 命令/方式 | 结果 |
| --- | --- | --- | --- |
| V1 | 导入/编译、删除模块无活动引用 | `uv run --no-project python -m compileall -q src/hyprial`；`rg` 检索旧 `gui/bundle`、`product_gui_bundle`、`gui-source.json`、`apps/gui` | PASS：compileall 无错误；无活动导入；仅剩测试负断言、说明文案和历史 `scripts/refactor/integration-contract.python.json`（已登记 R12） |
| V2 | CLI 启动注册、`hyprial gui` 明确不可用 | `test_gui_apps.py`、`test_gui_runtime.py`、CLI help inventory/coverage | PASS：本批 18 passed（生命周期/升级）+ 377 passed（含 help inventory/coverage、daemon start/unavailable）；`GUI_UNAVAILABLE` 扁平错误与 exit 1 有测试断言。手工 `env ... .venv/bin/hyprial gui status --json` 冒烟 BLOCKED（Bash 返回“This command requires approval”，未绕过，改由测试路径覆盖） |
| V3 | daemon/agent/message 核心冒烟 | `uv run pytest -q tests/shell/cli/communication`；`tests/shell/cli/test_cli_daemon_start.py`、`test_cli_daemon_unavailable.py` | PASS：communication 38 passed；daemon start/unavailable 与 CLI 注册在 377 passed 批次内；测试隔离，不触碰生产 daemon/用户数据 |
| V4 | 保留 adapter 测试 | `uv run pytest -q tests/shell/gui/adapter tests/shell/cli/presentation/test_cli_help_inventory.py tests/shell/cli/presentation/test_cli_help_coverage.py tests/shell/cli/test_cli_daemon_start.py tests/shell/cli/lifecycle/test_cli_daemon_unavailable.py` | PASS：377 passed |
| V5 | hook/Tailcat/worker-image 静态 | `test_gui_build_hook_worker_switch.py`、`test_tailcat_wheel.py`、`test_build_worker_image.py`、`test_worker_image_gitless_wheel.py` 静态臂 | PASS：hook/tailcat 42 passed；worker-image 静态 31 passed、1 deselected（真实 gitless build 属 V7） |
| V6 | fixture 依赖移除 | `test_upgrade_gui_unpack.py`、`test_snapshot.py`、文档测试、supervision/authority 测试；`ruff`、结构门、模块边界门 | PASS：升级/快照/文档 34+18 passed；supervision/authority 45 passed；`ruff check .` All checks passed；结构门 PASS；模块边界门 PASS（0 violations） |
| V7 | 真实 wheel/sdist、docker | 需先报告 CPU/内存/缓存/slot | `NOT_RUN`（重负载未执行；未获 slot） |
| V8 | 全量 pytest/五端打包 | 4GB WSL 与本次范围禁止 | `NOT_RUN` |
| V9 | PR A 纯路径删除 | PR #1999 / head `d9b953805`：`git diff --cached --name-status --diff-filter=MARCT`、`git status -uall`、`git grep app-env-preflight`、结构门；C 补丁干跑；A `base-freshness`/`submitted-by` | 344 paths 全部删除（`src/gui` 78 + `app` 262 + 4 app-only），0 新增/修改、无未跟踪；app 预检无剩余活动引用；当前基结构门预期 **FAIL**，仅 `missing-product-root: src/gui, app/src`（C PR #1997 head `d2a662bc` 已提交未合）。C 补丁干跑结构门 **PASS**（3464 files、945 modules、`violations: []`）；A 删除树模块边界门 **PASS**（1055 files、10475 imports、`violations: []`）；`base-freshness`/`submitted-by` 本地通过。未跑全量/真实构建/五端 |
| V10 | PR 路由探针与线上状态 | `node -e` 调用 `ci-plan.mjs component('src/hyprial/shell/impl/gui/runtime.py')`；核对 `ci-plan.mjs` PR 路由分支；`fj pr status 1998`/`1999` | `component(...) === 'gui'`；B #1998（target dev）按静态路由仍会被 `CI_ROUTE` 拒绝，C #1997 不改 planner；A #1999（target `intg/gui-retire-*`）路由可通过。06:45Z 线上 B（`44043baa3` 之后）为 `gui-unit` Failure、`tests/*` Pending。文档 head `1d04bc712` 的线上快照：`tests/pr-hygiene`、`contract-impact`、`skill-declaration`、`hosted-agent-e2e` Success；`gui/gui-unit`、`tests/cli-help-inventory`、`tests/checks`、`tests/unit` Failure；四个 pytest shard 与 `tailcat` Skipped。`checks` 的 fail-closed 汇总步在 `cli-help-inventory` 非 success 时会输出 `CI_ROUTE: upstream plan failed`，该形态与路由拒绝一致；未取得运行日志，不把它宣称为日志证据。B 落地 **BLOCKED** 待 operator 选路；未改 planner/未绕过 |
| V11 | PR-hygiene 本地门（不替代远端 CI） | B head `a7c532571`：`check_pr_hygiene.py merge-tree --base-ref dev`、`registry-union`、`submitted-by`；A head `d9b953805`：`base-freshness`、`submitted-by` | B merge-tree **PASS**（merge tree `7a91f37e5`；Node CI-policy/fast pytest green）、registry-union **PASS**、submitted-by **PASS**；临时 merge worktree 已移除。A base-freshness **PASS**、submitted-by **PASS**（远端 body 首行已正确，本地 body 文件随后补齐）。A 的 merge-tree 仍预期红，直到 C 合入并 rebase；远端 required 状态仍 `UNKNOWN`。最终文档 head `1d04bc712` 复跑：merge-tree **PASS**（merge tree `3115be0e1e9bcf0cd72befbe079a1ab79c63f2f0`；Node CI-policy 与快速 pytest 195 passed）、registry-union **PASS**、submitted-by **PASS**；同 head `test_open_source_project_docs.py` 8 passed；临时 merge worktree 已移除 |

验证合计：585 passed，1 deselected；未运行项不冒充通过。真实 GUI、真实授权/收发、
真实安装/升级/回滚、真实 wheel/docker 仍为 `NOT_RUN`。

## 恢复前反查清单

- 新 GUI 迁回前，按 R01–R15 反查旧消费者，不能存在未归属的必需恢复项。
- R05–R10 的 PR A 已提交为 #1999（head `d9b953805`，base
  `intg/gui-retire-20261027` = dev `3252c112`），344 个纯删除；合入前仍须核对
  `src/gui` 78、`app` 262、`test_tauri_handoff.py` 与 4 个 app-only 文件无遗漏，
  且 C/B 已就位。
- B #1998 不能直接合 dev：planner 仍将 `src/hyprial/shell/impl/gui/**` 归为 GUI 并
  要求 `intg/gui*` 路由；必须等 operator 选定受支持路径后才能合入，不得把“实现和 PR
  完成”写成“已可合入/已交付”。
- R11/R12 的 CI 悬空入口由 C/CI owner 处理后更新本表；本 worker 不得把 C 的改动
  记为自己的成果。
- 任何真实构建、发布、安装、签名、凭据或生产服务操作均不因本台账获得授权。


## R11 follow-up — A integration 2026-10-29

PR #2093, accepted and fast-forwarded into `intg/cli-command-surface` at
`45c8170e79f6e9d9ebd667373a47a549ba48061b`, retires
`.forgejo/workflows/desktop-check.yml` and the exact `desktop-unit` /
`desktop-rust` acquisition/tail audit entries because their `app/` and
`src/gui/` inputs were already removed. Active gate assertions remain; the
tail inventory is now 17 jobs, including 13 Ubuntu and 4 macOS jobs.
This supersedes the earlier R11 statement that desktop-check remains hanging.

`desktop-release.yml` still references the removed trees and remains a
manual release restoration item; it was not deleted or executed by A.
Neither this update nor the desktop-check retirement establishes GUI/native
release readiness. Required branch protection values were not altered.
If #2087 meets a modify/delete conflict at desktop-check, retain the reviewed
retirement; evaluate that integration against its own exact tree.
