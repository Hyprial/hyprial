# CI —— 容器单测与 macOS E2E

## 文件命名：前缀说明用途（2026-11-06）

| 前缀 | 含义 | 文件 |
|---|---|---|
| `gate-*` | 合并门，跑在 PR 上，其中的 context 应设为 required check | `gate-python.yml`（cli-help-inventory / tailcat-go / checks / pytest-shard / unit）、`gate-pr-hygiene.yml`（pr-hygiene）、`gate-pr-declarations.yml`（declaration / impact / changelog）、`gate-gui.yml`（gui-unit / gui-acceptance / release-package）、`gate-native-shell.yml`（launcher） |
| `e2e-*` 及 `mock-model-harness-e2e` | 端到端场景 | `e2e-macos-isolated.yml`、`e2e-macos-credentialed.yml`、`e2e-provider-calibration.yml`、`mock-model-harness-e2e.yml` |
| `release-*` | 发布链 | `release-tag.yml`、`release-nightly-promote.yml`、`release-main-dispatch.yml`、`release-install-scripts.yml` |
| `ops-*` | 运维辅助 | `ops-cancel-on-pr-close.yml` |

旧名对照：`tests.yml` → `gate-python.yml` + `gate-pr-hygiene.yml`；`gui.yml` → `gate-gui.yml`；
`skill-declaration.yml` / `contract-impact.yml` / `release-changelog.yml` → `gate-pr-declarations.yml`；
`e2e.yml` → `e2e-macos-isolated.yml`；`e2e-controlled.yml` → `e2e-macos-credentialed.yml`；
`provider-calibration.yml` → `e2e-provider-calibration.yml`；`hosted-agent-e2e.yml` → `mock-model-harness-e2e.yml`；
`nightly-promote.yml` / `main-release.yml` / `install-scripts.yml` → `release-*`；`cancel-on-pr-close.yml` → `ops-*`。
job 名不变，只有 `tests.yml` 的 `tailcat` 改为 `tailcat-go`。

⛔ **`wheel-release.yml`、`github-publish.yml`、`homepage-changelog-sync.yml` 暂不改名**：
其他 workflow 按文件名 dispatch 它们，目标 ref 是 `nightly` / `stable` / `v*` tag 或 `main`，
而这些 ref 上的 commit 早于改名，用新文件名 dispatch 会直接 404。等包含改名的版本发布、
所有 track tag 都指到新 commit 之后，再单独改名。

PR 的 pytest 只跑改动所在层及其上层的测试，规则见
[pytest 分层选择](../../docs/reference/pytest-impact-selection.md)。

## Dev-first migration (2026-11-08)

Completed PRs target dev; release selection uses a full SHA from dev history.
The common planner reads [`.forgejo/git-flow.json`](../git-flow.json).
All source branch names may target dev; complete file diffs select component
suites for PRs and dev landings, including mixed GUI/sidecar changes. Source
branch prefixes never exempt changed components from their suites.
The old source-branch restriction is retired. `release-main-dispatch.yml` remains
only as a manually invoked retirement diagnostic; it has no push trigger or
publication write. `release-tag.yml` keeps its explicit tag/sha/dry_run interface
and checks dev ancestry instead of main ancestry.

During 0.5.5.4 convergence, unit's `INTERNAL_PUBLICATION_ENABLED: "false"` freezes
both internal movement and dispatch. `FORMAL_PUBLICATION_ENABLED: "false"` in
release-tag blocks real tag creation while allowing dry-runs. Tests continue;
the independent installer publisher is unchanged. Neither hold is released by
a green PR or a static dry-run. Exact-candidate evidence and a separate reviewed
change are required; [release rules](../../docs/guides/release-rules.md) are the
authority. Existing publication concurrency, source SHA binding and lease guards
remain in force.

## Public distribution

分支与候选配置：[git-flow.json](../git-flow.json)；发布操作与验收：
[发布规则](../../docs/guides/release-rules.md)。三方产物必须一致，
下文只说明实现细节和恢复操作，不另设发布开关。

- `release-install-scripts.yml`: `dev` installer changes/manual dispatch → four R2 root
  objects; separate unsigned drift check against the same checkout. No PR upload.
- `wheel-release.yml`: exact-source five-platform build → verified direct R2
  publication → packages/GitHub. All three tracks are enabled by default. The
  source remote's peeled track must still equal the immutable build SHA before
  uploads and before each manifest PUT attempt; stale runs fail without switching
  SUMS. This closes sequential stale retries, not the final read/write race.
- CI tag moves explicitly dispatch wheel builds before their independent GitHub
  requests. Internal uses the mover's `promoted` output; nightly uses its existing
  output; formal releases compare the track pointers before/after reconciliation.
  No-op moves do not dispatch new wheels. A failed dispatch is reported; operators
  can retry the wheel workflow manually with the approved track and full source SHA.
- R2 secrets are scoped to upload steps: `R2_ENDPOINT`, `R2_BUCKET`,
  `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`. There is no enable/disable variable.
  Full-bucket before/after scope checks require listing permission and can detect
  unrelated concurrent writes as failures; these are not atomic bucket updates.

### Release wheel 的 GUI source remote（2026-11-16，owner 决定）

发布 wheel 的 `hyprial/_gui/release.json` 必须把公开仓库地址记为 `sourceRemote`，
而不是构建者 checkout 的 `remote.origin.url`（内网主机）。唯一输入是环境变量
`HYPRIAL_BUILD_SOURCE_REMOTE`，只由 wheel 构建钩子在 checkout 构建时读取，只接受
`PUBLIC_SOURCE_REMOTE`（定义在 `src/hyprial/shell/impl/gui/install/resources.py`，
`github_publish.py` 的 `DEFAULT_TARGET_REMOTE` 由测试钉成同值）；空值、带凭据或任何
其他值、以及在无 `.git` 的快照树上设置都是构建错误。不设置则保持 checkout remote，
内部开发者的本地 wheel 仍可用 `hyprial gui dev`，钩子会在 stderr 提示“仅限本地、不得分发”。
`sourceCommit` 不变。

逐 job 决定（策略测试 `tests/tooling/ci/tailcat_wheel/test_wheel_builds_record_public_remote.py`
对未声明的构建 workflow 红灯，对发布构建缺变量红灯）：

- `wheel-release.yml` 的 `Stamp local version segment and build wheel`：五个目标 wheel
  发布到 R2 / packages / GitHub release 附件，**设置**该变量。
- `github-publish.yml` 的 installability 闸：在无 `.git` 的快照里构建，wheel 只在 job 内
  验证后丢弃，来源由 `gui/gui-source.json` 的 stamp 决定；**不得设置**（设置即构建错误）。
- `kp027-native-candidate.yml` 全部构建（worker wheel 不含 GUI；product wheel 与 darwin
  host wheel 仅作为同一 run 的 evidence artifact 上传，该 workflow 无 publish/attach
  命令）：**不设置**，策略测试断言其不含发布命令；若将来晋升为发布物，须先在其构建步骤设置变量。
- `release-tag.yml` / `release-nightly-promote.yml` / `release-main-dispatch.yml` 不构建
  wheel，只 dispatch `wheel-release.yml`；`scripts/build/build-wheel.sh` 是本地脚本，
  继承调用者环境，不设置。

兜底：`scripts/ci/wheel_source_remote.py` 打开 wheel 内 `hyprial/_gui/release.json`，
`sourceRemote` 不是公开地址即拒绝；`verify_tailcat_wheels.py`（发布前五件套校验）、
`publish_wheels.py`（R2 上传前）与 `attach_wheel_assets.py`（下载 registry 件后、任何上传前）
都调用它，因此即使某个 workflow 漏设变量也发布不出内网 remote 的 wheel。

### Installer bootstrap and recovery dispatch

A newly merged workflow is not proof that Forgejo created a run. After an eligible
`dev` push, check Actions for an `install-scripts` run at that exact merge SHA.
If none exists, an authorized operator must open **Actions → install-scripts →
Run workflow**, select **dev**, and dispatch once. Do not dispatch a tag or PR
branch: both jobs require `refs/heads/dev`. If the UI does not list the workflow,
use the authenticated workflow-dispatch API (see the forgejo-ops skill), then
check registration/run creation; do not remove the dev guard or repeatedly send
requests without checking the result.

Record the run ID and resolved SHA. Require both `publish` and `drift` to pass,
then verify the four root installer objects against that checkout. This publisher
does not produce `data/install.json`; do not claim its recovery from these jobs.
Dispatch acceptance alone is not upload success. Missing objects remain
FAIL/BLOCKED until a real run verifies them. This fix does not dispatch a run.

See [wheel distribution](../../docs/guides/wheel-distribution.md) for installation,
object ordering, drift verification, recovery and remaining live acceptance.

## 单元测试
`gate-python.yml` 在 push/PR 到 dev/main 时,由实例级 DinD runner 的 `ubuntu-latest`
标签启动隔离 Linux 容器,运行全量 `pytest tests/`。当前标签映射到
`node:22-bookworm`;workflow 固定安装 uv 0.11.7,并用 Python 3.12 执行测试；
`macos-host` 不再承担单元测试。

任务结构（2026-10-04 CI 提速）：

| 任务 | 内容 | 是否必需检查 |
|---|---|---|
| `cli-help-inventory` | 计划（`ci-plan.mjs`）、结构门、CLI help 清单 | 是 |
| `checks` | lint、编译、CI 选择测试；docs-only PR 的文档门；只改 GUI 时的审计子集 | 否（由 `unit` 汇总） |
| `pytest-shard (1..4)` | 全量 pytest 按文件分 4 片并行，`scripts/ci/pytest_impact.py --shard K/4` | 否（由 `unit` 汇总） |
| `pr-hygiene` | PR 卫生四规则：base 新鲜度、merge tree 结构门 + CI 策略测试 + 两个廉价树级 pytest 门、registry union、`Submitted-by` 身份行；只在 `pull_request` 上运行 | 是（由 `unit` 汇总） |
| `unit` | 汇总：计划、`checks`、全部切片都符合预期才绿；只在 dev push 上移动 `internal` tag | 是（`gate-python / unit*`） |

### Integration lane 的轻量 PR 门

以 `intg/batch-*` 为 base 的 PR 只运行轻量 CI：路由 planner、`pr-hygiene`、
`Skill-Impact` 与 `Contract-Impact` 声明门继续执行，4 个 pytest shard、GUI、
package 与其他重套件不启动。`intg/batch-*` 合入 `dev` 的 PR 仍执行完整门禁；
`intg/gui*` 与 `intg/sidecar*` 仍执行各自 component suite。

### PR 卫生门（`pr-hygiene`）：独立 workflow，独立 required context

2026-11-06 之前它放在 tests.yml 里，挂在 `unit` 的 `needs:` 上，靠 `tests / unit*`
阻塞合并（`needs:` 只在同一 workflow 内生效）。拆分后它在 `gate-pr-hygiene.yml`，
阻塞只靠它自己的 context：**`gate-pr-hygiene / pr-hygiene*` 必须是 dev 和 main 的
required check**，否则红了也能合并。`gate-python / unit` 不再读取它的结果，
它红也不再跳过内容 job。

四条规则实现于 `scripts/ci/check_pr_hygiene.py`，配置在
`scripts/ci/registry-union.json`（rule 3）与 `scripts/ci/pr-identity.json`
（rule 4 的创建时间 cutoff + 显式 PR 白名单）。规则脚本本身**从 base ref 取权威副本**：job 在 checkout 之后先运行
`git show origin/<base>:scripts/ci/check_pr_hygiene.py > scripts/ci/check_pr_hygiene.py`，
四条规则因此是「base 版脚本审候选树」，候选树里的同名脚本不参与判定
（kanban `1d076332`）。这一条只闭合「**脚本由候选提供**」这一面；
**工作流定义面未闭合**——`gate-python.yml` 与 `ci-step.sh` 仍取自候选树
（`edited` 触发就是候选头自己加上的：#1448 / run 25818），残余记在该卡上，
⛔ 不当成已闭合。rule 1 只在 base 为 `intg/*`
时运行；rule 2 在 worktree 里 `git merge --no-commit` 物化 merge tree 并
在那里跑 `check_structural_limits.py` / `check_module_boundaries.py`、
`node --test scripts/ci/*.test.mjs`，以及两个**纯 stdlib + pytest** 的树级门
（`tests/tooling/architecture/lint/test_provider_term_lint.py`、
`tests/integration/native_harnesses/test_native_resident_tripwires.py`
——`NATIVE_ORIGINS` 是 `resident_metadata/native_origins_*.py` 各分片的并集，重复键直接红——
命令带 `--noconftest`，因为仓库级 `tests/conftest.py` 会 import `hyprial.*`
并拖入整锁环境；这两个文件今天不依赖 conftest 的 fixture 与 fd prewarm，
一旦依赖，等价性即失效——完整环境对照归 e2e 的装配步，不在这里），
冲突直接红；rule 4 的过渡豁免是固定
常量，不依赖运行时时钟：创建早于 cutoff 的存量 PR **永久 warn-only**，
后续 push 不会自动升级，只有 `upgrade_pr_numbers` 里显式点名的 PR 号才转红。
失败信息与 e2e 的分类器同词表，分七态：`ok`、`present-but-not-first-line`、
`missing-wrong-case`、`missing-no-agent-prefix`、`missing-empty-uri`、`missing`、
`blocked-unclosed-comment`；每条近形都点名「看到了什么」并引用越界的那一行
（模板占位符 `agent:<URI>` 仍按 `missing` 拒绝，这是与 e2e 前缀级读法的有意分歧）。

工作流策略另有两条禁入断言（测试即机制，见
`tests/tooling/ci/execution/test_ci_workflow_policy.py`）：`pull_request_target`
禁止（基础仓 secrets + 写权限自动 token 对上不可信 PR 代码），`permissions:` 禁止
（Forgejo 16.0.5 静默忽略该键，制造虚假安全感；上游 `forgejo#13079` 落地后该断言必须
**反转**为要求 `permissions: contents: read`，反转条件写在失败文本里）。

声明门按行匹配，所以「首行 `Submitted-by` + 后续声明行」是兼容的；
`Skill-Impact` 只接受两种取值（写成 `updated: <自由文本>` 会红）：

```text
Skill-Impact: updated
Skill-Impact: not-needed: <理由>
```

这类 integration lane PR 的正文必须提供本地实测，供 e2e-verifier 作为 gate ④：

```text
Local gates:
- <command> — PASS (test paths: <paths>; pass/skip: <passed>/<skipped>)
```

每条都写实际命令、测试路径、退出结果以及 pass/skip 数；未运行或受阻的项目明确写
`NOT_RUN` / `BLOCKED`，不能把 skip 当作通过。

切片规则：
- 以文件为单位，结果确定、互斥；第 4 片是兜底，执行 `tests/` 并忽略前 3 片的文件，所以新增或未知的测试文件一定会执行一次。
- 分片权重来自 `scripts/ci/pytest-durations.json`，文件内注明了来源和刷新方法。权重只影响均衡，不影响跑哪些测试。
- `unit` 用 `always()` 只是为了能报告上游的红；它的第一步会把失败、取消、缺失或意外 skipped 的切片判为红，因此不会默认变绿。

依赖缓存：
- uv 的 wheel 缓存放在 runner 的持久卷 `$CI_CACHE_ROOT/uv-wheels`，由 `scripts/ci/ci-uv-cache.sh` 选择；
- 每个任务的日志里有一行 `CI_UV_CACHE ... mount=... writable=... entries_before=N`，用来核对挂载、可写和跨任务可见；
- 挂载不成立时退回本任务的内存盘，并给出 warning；
- 测试临时目录始终留在 `/ci-tmpfs` 内存盘，持久缓存不替代它。

pytest 使用每次 run 唯一且较短的
`/tmp/hyprial-pytest-${GITHUB_RUN_ID}` 作为 basetemp。测试前会打印 `uname`、Python
解释器与版本、multiprocessing start method、`TMPDIR` 和 basetemp,便于区分容器
环境漂移与产品回归。

单测步骤设 `NO_COLOR=1`,避免容器内 ANSI 转义拆散 help 文本断言(#118)。
每个切片会上传 `pytest-groups-pytest-shard-<n>`（junit 和慢测试列表），用于刷新分片权重；
job log 仍然是测试记录本身。含 FD 探针的切片分两波执行：`fd-probe` 波以单 worker 运行嵌套探针，
该切片其余文件保持声明的 `-n 2`，因此该切片的产物里有两个 junit
（`unit.xml.fd-probe.xml` 与 `unit.xml.shard-<n>.xml`）；刷新分片权重时两者都要传给
`--write-durations`，只传一个会漏掉波内文件的权重。

## GUI 检查名称与范围日志

`gate-gui.yml` 的 Node 单测 job 为 `gui-unit`，与 `gate-python.yml` 的 Python `unit`
区分；状态应按 workflow + job + 精确 SHA 判读，不能只按裸 job 名或耗时判断。
GUI 的 scope 输出仍叫 `unit`（`steps.scope.outputs.unit`），这是选择器协议，
不是 job 名。重命名不改变触发条件、依赖关系或测试步骤。

根目录与 GUI 包的 `ci-scope.mjs` 入口共用同一选择器，每次 CLI 调用输出一行
`CI_SCOPE <JSON>`，包含 `project`、`event`、各 gate 的布尔选择及 `reason`。
该行表示选择了哪些检查，不表示这些检查已执行或通过。文档/浏览器范围选择、
main/dev push 全量检查以及比较不可用时全量回退的规则均保持不变。

本变更不修改服务器分支保护，也不修复 required context 与事件后缀的匹配。
外部状态引用若依赖旧 GUI job 名，需单独核对；不要将 GUI 检查替代 Python 全量检查。

## 原生壳 launcher 门（`gate-native-shell.yml`，2026-11-16）

只在 `clients/native/**`、`contract/cli/**`（launcher 解析的启动记录语料）或本 workflow 改动时由
`paths` 过滤触发，单个 job `launcher`（`ubuntu-latest` + `node:24-bookworm` 容器）：
`scripts/ci/ci-ensure-rust.sh` 安装仓库固定的 Rust 1.93.1（与 native-conformance 同一安装器，
不用浮动 action），再用它引导的 rustup 给同一工具链补 `rustfmt`、`clippy`，然后对不依赖 Tauri 的
`hyprial-shell-launcher` crate 跑 `cargo fmt --check`、`cargo test --locked`、
`cargo clippy --all-targets -D warnings`。job 带共享的 CI conditions 首尾步骤。
Tauri crate（`clients/native/src-tauri`）需要 webview SDK，不在 Linux 上构建；它的构建与
冒烟在 macOS arm64 主机上做（`docs/design/gui-single-launch-plans-2026-11-15/shell-6.md` §E.2）。
本 workflow 只靠 `paths:` 触发，未改动这些路径的 PR 不会产生它的状态，因此**不能设为 required check**（否则那些 PR 会永远等不到这个状态）。`ci-plan.mjs` 的选路不因本 workflow 改变。

## 原生壳 macOS 打包：不在 CI 里（2026-11-20）

owner 2026-11-20 决定：DMG 打包完全离开 CI，只需在打包 Mac 上手动运行脚本 `scripts/build/package-macos-shell.sh`
（ad-hoc，或带 `--developer-id` 签名、公证、staple）。两个打包 workflow 都已删除：`shell-macos-candidate.yml`
（2026-11-16 加入、2026-11-17 改为手动薄调用的 ad-hoc 候选）与 `shell-macos-signed.yml`（2026-11-19 加入的手动签名 workflow）。
这推翻了 GUI 线此前要求的单独签名 workflow。现在没有任何 workflow 调用 `package-macos-shell.sh` 或
`packaging/clients/sign_bundle.py`、传 `--developer-id`、签名或公证；
`tests/tooling/release/shell/signing/test_signing_only_by_switch.py` 对此做回归检查（不是安全边界：签名身份仍在 runner
进程所用账号的登录 keychain 里，“CI 不签名”靠的是没有 workflow，KP-100）。原来由 workflow 提供的东西改由操作者给出：
主线上的源提交（祖先检查只存在于已删除的签名 workflow，现在由操作者核对）、`HYPRIAL_BUILD_SOURCE_REMOTE`
设为 `PUBLIC_SOURCE_REMOTE`、可选的 `--tauri-cli-dir`。步骤见 `packaging/clients/README.md` “Package by hand”。
`kp027-native-candidate.yml`、`native-conformance.yml` 等仍在 `release-macos-arm64` 上运行，不受影响。

## Native conformance 选路与汇总

`native-conformance.yml` 的 `plan` 与 Python/GUI 门共用 `scripts/ci/ci-plan.mjs`，
按完整 PR merge-base diff 输出 `native`。只有 ABI/引擎及其构建依赖、共享或未知输入、
显式全量入口选择原生矩阵；普通文档（含 `ROADMAP.md`）以及无关 GUI/Tailcat 改动不选择。
`GLOSSARY.md`、Squire Skill 模板等测试输入仍选择 Python，不被当成纯文档豁免。
比较失败使 `plan` 和 `native-summary` 失败，不能生成文档-only 的绿灯。

原有 `reference-abi`、`native-linux-amd64`、`native-linux-arm64`、
`native-darwin-arm64`、`free-threaded-3-15t` 五个 suite job 名不变（P8 删除了
pykka job），统一消费 `plan`。`native-summary` 要求计划成功；未选择时五个作业必须
都是 `skipped`，选择时四个阻断作业必须成功，3.15t 保持已有非阻断策略但不得缺失或被取消。
`NOT_SELECTED` 只表示本次差异
不在原生 ABI 范围，不是测试通过。

### arm64 lane 只跑 dev push / 计划任务 / 手动 / 发版 PR（2026-11-15，owner 批准）

`native-linux-arm64`（QEMU 模拟，单 runner，单 job 约 45 分钟）和 `native-darwin-arm64`
（单台 macOS runner）排队最长，PR 上的取消率也最高，所以它们只在 `plan` 输出
`native_arm64=true` 时运行。该值由 `scripts/ci/ci-plan.mjs` 的 `makePlan` 统一给出（唯一位置）：
`native_arm64 = native && (event != pull_request || base 为 main 或 release/**)`。
即 dev 的 push（该 workflow 新增 `push: branches: [dev]`）、schedule、workflow_dispatch，
以及目标为 `main` / `release/**` 的 PR（发版门需要合并前的 arm64 证据）在选中原生范围时运行；
目标为 dev、`intg/**`、`intg/batch-*` 等其它分支的 PR 不运行。

`native-summary` 语义：arm64 被选中时（含发版 PR），`reference-abi`、三个原生平台作业必须成功，
输出 `PASS mandatory=4/4`。arm64 未选中（其它分支的 PR）时，`reference-abi`、`native-linux-amd64`
必须成功，两个 arm64 作业必须恰为 `skipped`（成功或失败都报红 `unexpected-arm64-job-*`），
输出 `native-linux-arm64=NOT_RUN native-darwin-arm64=NOT_RUN ... event=pull_request` 与
`verdict=PARTIAL_NO_ARM64 mandatory=2/4`，退出 0；它**不是** `PASS`，arm64 证据来自 dev push 或发版 PR。
`NATIVE_ARM64_SELECTED` 缺失/不是 true|false，或 native=false 却 arm64=true，汇总直接红（fail closed）。

### WIP 拉取请求只跑轻量检查（2026-11-15，owner 批准）

**判定只有一处**：`scripts/ci/ci-plan.mjs` 的 `wipDecision`，只读本次事件的 payload：
`pull_request.title` 以**区分大小写**的 `WIP:` 开头，或 payload 带 `pull_request.draft === true`。
目标为 `main` / `release/**` 的 PR、push、schedule、workflow_dispatch 永不判 WIP。
区分大小写是有意的：Forgejo 默认 `WORK_IN_PROGRESS_PREFIXES = WIP:,[WIP]` 且**不区分大小写**，
所以 `wip:`、`[WIP]` 这类 Forgejo 也拦合并、而我们不认的标题只会跑**完整**检查（安全方向）；
反过来不会出现我们当 WIP、Forgejo 却放行合并的标题。每个计划作业的日志有
`CI_WIP action=… wip=true|false reason=…` 一行（gate-python 的 `cli-help-inventory`、
gate-gui 的 `gui-unit`、native-conformance 的 `plan`、mock-model-harness-e2e 的 `plan`）。

WIP 计划 = 既有的轻量计划（`light=true`，所有 suite 为 false）加 `wip=true`；`makePlan`
只校验 checkout 等于事件 head，不做 diff 比较。其余 workflow 只按这个输出行动：

| 作业 | WIP PR 上的状态 |
|---|---|
| `gate-pr-declarations`（declaration / impact）、`gate-pr-hygiene / pr-hygiene` | 照常运行（本来就是轻量门） |
| `gate-python / cli-help-inventory` | **运行，但只做计划**：checkout、计划、共享策略 node 测试、docs-only 分类；CLI 清单/契约/结构步骤因 `full/python=false` 跳过。它是计划作业，不能整体跳过 |
| `gate-python / checks`、`unit` | **skipped**（`if` 要求 `wip != 'true'`；计划失败时 `wip` 为空，二者照旧运行并变红） |
| `gate-python / pytest-shard`、`tailcat-go` | skipped（`python/sidecar=false`，既有条件） |
| `gate-gui / gui-unit` | 运行但只做计划（重步骤只随 `gui=true`）；`release-package` skipped |
| `mock-model-harness-e2e / plan` | 运行（几秒，只读 payload）；`hosted` skipped |
| `native-conformance / plan`、`native-summary` | 运行；五个 suite 作业 skipped；汇总输出 `verdict=NOT_RUN reason=wip-pull-request`，不是 PASS |

**重型 context 在 WIP PR 上的状态是 skipped（job 级 `if` 为假），没有绿色成功**。`unit` 在 WIP 上
被跳过而不是以轻量计划发绿，正是为了不发布「没有任何 suite 证据的 `unit` 成功」。Forgejo 自己拒绝
合并 WIP PR，是第一道保险；去掉 `WIP:` 后见下。**假设（需合并后在线核对）**：job 级跳过在 Forgejo 里
记为 skipped 状态而不是 success，且一个更晚 run 对同一 context 的状态覆盖更早的。

**去掉 `WIP:`**：gate-python、gate-gui、native-conformance、mock-model-harness-e2e 的
`pull_request` 触发都加了 `edited`。Forgejo 文档（`on.pull_request`）列出的 types 为
opened、reopened、closed、labeled、unlabeled、synchronize、assigned、unassigned、edited
（「标题或正文被修改」）；**没有** `ready_for_review` / `converted_to_draft`，因此
`gate-pr-declarations.yml` 里已有的 `ready_for_review` 很可能从不触发（本次不改，只记录）。
改标题触发 `edited`，其 payload 里的新标题不再带前缀，计划即为同一 head 的完整计划，无需新 push。
实例 act_runner 为 v12.13.2（来自作业日志），服务端 Forgejo 版本未核实，`edited` 是否对 PR
标题变更触发、以及 payload 是否带 `changes` 都是**假设**（见下）。

**不改 WIP 状态的编辑**（只改正文、或改标题但保持 WIP/非 WIP）：payload 的 `changes` 理论上可区分，
但跳过这样的 run 会让它的 skipped 状态覆盖同一 head 上更早完整 run 的状态，所以**不跳过**：
它按当前标题重新计划。代价：非 WIP PR 的正文编辑现在会重新排一轮完整检查（此前 gate-python
不听 `edited`）。若实测负载不可接受，下一步是让这类编辑只在 plan 里 `exit` 而不发布 suite 状态，
前提是先用在线检查 3 确认状态覆盖语义。

**并发**：各 workflow 的 concurrency group 不含事件 action，同一 PR 的轻量 run、`edited` 的完整 run、
后续 push 的 run 同组且 `cancel-in-progress`：更新的 run 取消更旧的。最新事件的标题决定计划，
所以不会出现完整 run 被一个更晚的「更轻」run 取消后 PR 没有完整 run 的情况（更晚的 run 总是按当时
标题计划）。反向（完整 run 进行中被改成 WIP）取消完整 run 是期望行为。`mock-model-harness-e2e`
本来 `cancel-in-progress: false` 且无 per-PR 组，不受影响。

**合并后必须在线核对（假设清单）**：
1. 在测试 PR 上把标题改成 `WIP: x` 再改回 `x`，不推送：确认每次改标题都产生 `pull_request` 的
   `edited` run（Actions 页的 event/action），去前缀后 gate-python 的 `unit` 与 pytest 分片确实排队运行。
2. 在 WIP PR 上看提交状态页：`gate-python / checks`、`unit`、`native-conformance / *` 的 suite 作业是
   skipped（或不存在），不是 success；`native-summary` 日志含 `verdict=NOT_RUN reason=wip-pull-request`；
   `CI_WIP ... wip=true` 出现在计划作业日志。
3. 去前缀后，同一 head 上上述 context 被新 run 覆盖为真实结果；在 `unit` 为 required 的分支上
   确认合并按钮在去前缀后的完整 run 绿之前不可用，并确认 skipped 不被当作满足 required。
4. 只改正文触发一次 `edited`：确认 `mock-model-harness-e2e` 在**未改动**其 `paths` 的 PR 上没有被
   带起（paths 过滤是否对 `edited` 事件生效是假设），并记录这次完整 run 的实际负载。
5. dev push 上 `native-conformance` 新增的 push 触发：确认 `native-linux-arm64`、`native-darwin-arm64`
   只在选中原生范围的 dev 落地上排队。

### Rust 工具链下载的有界重试（2026-11-15，owner 批准）

实测：dev push 上的 `gate-python / pytest-shard (2)` 在 77 秒红灯，原因是固定 Rust 1.93.1 的
下载在 `static.rust-lang.org`（`channel-rust-1.93.1.toml.sha256`）遇到 TLS 握手 EOF；几分钟后
同一步成功。`curl --retry` 不重试 TLS 握手失败，所以 `scripts/ci/ci-ensure-rust.sh` 现在用
`scripts/ci/ci-retry.sh` 的 `ci_retry` 包住且只包住三处网络取回：rustup-init 下载、官方
distribution 暂存（`ci-rust-dist.py`）、`rustup toolchain install`。最多 3 次，退避 2 秒、5 秒
（`CI_RETRY_BACKOFF` 仅供测试覆盖），每次失败打印一行
`CI_RETRY label=… attempt=n/3 status=failed rc=…`，最后失败打印 `exhausted` 并以非零退出
（`e2e-rust: … failed after 3 attempts`）。版本固定为 1.93.1、rustup 1.28.2，校验和与版本探测
都在重试之外，所以错误摘要或版本只会立刻失败，不会被重试洗成通过。已有 12 分钟的 step 上限
不变；三次都卡满时由该上限截断。`scripts/build/manylinux_native_evidence.sh` 是手动证据脚本，
不在 CI 路径上，未改动。

### uv 环境创建的有界重试（2026-11-17，owner 批准）

实测（2026-11-17 03:10–04:30 UTC）：多个 PR 与 dev 上共 57 个 CI 任务在 Python 环境步骤失败，原因是 runner
到 PyPI / GitHub release 下载的出站连接间歇性失败（成功与失败在同几分钟内交错）。按失败 URL 分：
`pypi.org/simple/hatchling/` 20、`pypi.org/simple/pytest/` 17、其他 `error sending request` 14、
`Failed to download <pkg>==<ver>` 约 14、`github.com/astral-sh/python-build-standalone/…/cpython-3…` 6。
uv 默认 3 次重试约 29 秒就放弃，而抖动更长。持久缓存当时是在的，但仍要联网：`uv sync` 现建项目时要用
索引解析构建依赖 `hatchling`（不在 `uv.lock` 里），`--with pytest` / `--with libcst==1.9.0` 覆盖层同样要查索引，
自由线程 CPython（3.14t / 3.15t）则每次都 `unset UV_PYTHON_INSTALL_MIRROR` 后直连 GitHub。
`gui-unit` 与 `cli-help-inventory` 此前没有接持久 uv 缓存，每次冷启动。

两层有界重试，并改接缓存：

- uv 内部：每个跑 uv 的 job 在 `UV_HTTP_TIMEOUT` 旁设 `UV_HTTP_RETRIES: "10"`（macOS host 模式 job 也设，只是读取型设置）。
  用固定的 0.11.7 二进制实测：对拒绝连接，3 次约 4.6 秒，5 次约 12 秒，8 次约 37 秒，10 次约 62–66 秒
  （指数退避加抖动，设置同样作用于 `uv python install` 的 CPython 下载）。真实 TLS 握手 EOF 每次请求还要自己耗时，
  估计单条 uv 命令总共 2 分钟量级。`scripts/git_environment.py` 的非受信环境白名单同步放行 `UV_HTTP_RETRIES`，
  所以 pr-hygiene 合并树闸里的 `uv run --with pytest` 也吃到它。
- uv 外部：`scripts/ci/ci-retry.sh` 新增 `ci_retry_uv LABEL COMMAND…`（最多 3 次，`CI_RETRY_BACKOFF` 默认 `10 30`，
  每次失败打印 `CI_RETRY label=… attempt=n/3 status=failed rc=…`）。只包住「取回并物化环境」的幂等命令：
  `uv sync --locked …`、`uv python install …`、`uv pip install 'pytest>=8.4,<9'`，以及为后续验证命令预热缓存的
  `uv run … python -c 'pass'` / `import pytest` / `import libcst`（参数与随后的验证命令一致，不加任何标志，装出来的
  东西与未包装时完全相同）。测试、lint、闸门命令不包；`tests/tooling/ci/uv_resilience` 用白名单正则钉住。
  uv 对网络失败与非网络失败都退出 1 或 2（0.11.7 实测：拒绝连接下载时 1，校验改过的 lock 时 2，无法解析时 1），
  退出码无法区分，所以锁不一致之类的非网络失败也会被重试，最多再 2 次、约 40 秒退避，并逐次打印 `CI_RETRY` 行，不会被洗成通过。
- 取回 uv 发行归档（`ci-ensure-uv.sh` 缓存层）与 CPython 归档（`ci-ensure-cpython-mirror.sh`）的 `curl` 同样由
  `ci_retry_uv` 包住，只包下载，固定 sha256 的校验在重试之外。
- 自由线程 CPython：`ci-ensure-cpython-mirror.sh` 接受参数 `3.14t` / `3.15t`，按 uv 0.11.7 自己解析出的 3.14.4 与
  3.15.0a8（release 20260414）暂存到同一个 `$CI_CACHE_ROOT/cpython` 镜像，pin 取自该 release 发布的 SHA256SUMS。
  Linux 的 `native-linux-amd64`、`native-linux-arm64`、`free-threaded-3-15t` 不再 `unset UV_PYTHON_INSTALL_MIRROR`；
  macOS host 的 `native-darwin-arm64` 没有 `CI_CACHE_ROOT`，保持 `unset`，只加重试。`free-threaded-3-15t` 另接持久 uv 缓存。
- `gui-unit`、`cli-help-inventory` 现在在同步前接持久 uv 缓存（`ci-uv-cache.sh`）；`pr-hygiene` 在合并树闸之前
  新增预热步骤（Python 3.12 与 pytest 覆盖层）；`pytest-shard` 在同步后预热 `libcst==1.9.0` 覆盖层。

最坏时间（索引真的挂了）：快速失败（拒绝连接或握手 EOF）时，一条被包装的命令约 3 × (uv 约 65 秒退避加每次请求耗时) + 40 秒，
约 4–7 分钟；被包装的步骤上限都不小于 8 分钟（pr-hygiene 预热 8、gui 同步 10、gate-python 12、native 20、free-threaded 15–20），
且不超过各自 job 的 `timeout-minutes`。请求挂死时由 step 上限截断，不是由重试截断；本变更没有提高任何超时。
`tests/tooling/ci/uv_resilience` 断言每个被包装步骤的上限在 8 分钟到 job 上限之间。

未采用「先 `--offline`，缓存未命中再联网」：构建依赖（`hatchling`）不在 `uv.lock` 里，离线时 uv 用共享缓存里
已有的索引元数据解析，可能选出与联网时不同的构建后端版本，违背「不改变装什么」；锁内的 wheel 本就由 `uv.lock` 的哈希校验。
这不改变根因：runner 到 PyPI / GitHub 的出站网络是基础设施问题，本变更只让环境步骤扛过几分钟的抖动。

### gate-gui 的 Python host 一致性用例（2026-11-16 起并入 `Run independent GUI tests`）

2026-11-15 曾有独立步骤 `Run GUI host conformance against the Python host`
（`npm --prefix gui run test:host-conformance:python`，`HYPRIAL_HOST_IMPL=python`），因为当时同一套黑盒
host 用例还要对 Node host 跑默认一遍。Node host 与 `HYPRIAL_HOST_IMPL` 已于 2026-11-16 删除，这些用例
（`host.test.ts`、两个 loopback 文件与 `session-contract.test.ts`，来源 `gui/tests/daemon-request-host/`）
现在只有一个 host，并直接在 `npm --prefix gui test` 内运行：它们经 `gui/tests/support/python-host.ts`
启动产品 Python host 子进程（`.venv` 解释器，由 `Sync Python environment for GUI integration tests`
的 `uv sync --locked` 提供），用例逐条对照 `contract/gui-session/`。独立步骤因此删除，不再重复运行；
`gui/package.json` 的 `test:host-conformance` 只是开发者本地单独运行这组用例的命令，不在 CI 路径上。
`Verify source-mode GUI lifecycle` 同时运行 `tests/shell/gui/host/`，使 Python host 的 pytest 单测与
语料等同性检查也在本 job 内执行。`tests/tooling/ci/planning/gui/test_root_gui_workflow.py` 钉住这一点。

### gate-gui 的 npm 缓存与 pinned Chromium 缓存（2026-11-10）

`gui-unit` 与 `release-package` 在每次 `npm --prefix gui ci --ignore-scripts
--no-audit --no-fund --prefer-offline` 之前运行共享的 `Persist the npm cache`
步骤（内联 shell，无 helper，不触碰 PATH）：`CI_CACHE_ROOT` 设置时把
`npm_config_cache` 指到 `$CI_CACHE_ROOT/npm`（与 `gate-python.yml` 同一卷；
npm cacache 内容寻址、可并发写），未设置时打印
`CI_CACHE_ROOT unset: npm cache stays job-local (cold)` 并留在任务本地；
同时写入 `npm_config_fetch_retries=5`、`fetch_retry_mintimeout=10000`、
`fetch_retry_maxtimeout=120000`、`fetch_timeout=60000`。该步骤只配置缓存与
fetch 重试，其日志行不是测试结果。

`Ensure a CJK font and fontconfig for the browser screenshots` 位于 Chromium 步骤之后、
`Run independent GUI tests` 之前（2026-11-15）：`scripts/ci/ci-ensure-cjk-font.sh` 在
`fc-list ':lang=zh'` 已有字体时为 no-op，否则 `apt-get install -y --no-install-recommends
fonts-wqy-zenhei fontconfig`（Debian 主机沿用作业的代理变量）；随后工作流再断言
`fc-list ':lang=zh' | grep -q .`。原因：`fc-match :lang=zh-cn` 在没有 CJK 字体时也会返回
某个字体（只装 Liberation 时返回 LiberationMono），截图测试因此只采信 `fc-list ':lang=zh'`
列出的文件（`gui/tests/dev01-visual-font.mjs`，单测以注入的命令输出验证选择逻辑）；不再依赖
Chromium 步骤的副作用装字体。决策记录在本段与脚本头部，工作流文件中不写行内注释。

`Provision pinned Chromium for the browser gates` 位于 `Run independent GUI tests`
之前（2026-11-15：`dev01-onboarding-browser.test.mjs` 需要浏览器，测试步骤设置 `CI=true`，
此时缺少浏览器是失败而不是跳过），调用
`bash scripts/ci/ci-playwright-cache.sh "${RUNNER_TEMP:-/tmp}/hyprial-playwright"`
（参数只是非持久环境下的 job-local 回退目录）。脚本契约：

- 缓存按 `gui/package-lock.json` 的 playwright-core 精确版本 + OS 代号
  （`VERSION_CODENAME`，如 `bookworm`）+ dpkg 架构隔离；任一维度不同即不同目录，互不复用；
- 复用缓存树前必须通过真实启动探针（headless 启动浏览器并开页）；
- 探针失败或缓存缺失时回退为完整 `playwright install --with-deps chromium`
  （容器内装共享库）；
- 并发 job 用 flock 串行化安装/回退，避免半态目录被并发消费；
- `CI_CACHE_ROOT` 缺失、不在持久挂载下、或只落在易失 fs（`tmpfs`/`ramfs`/`overlay`，
  这些可写但内容不跨 job 保留）时，回退 job-local 目录并显式 warning。探测
  思路与 `ci-uv-cache.sh` 相同：看 `/proc/mounts`，不轻信变量，并读取第三列
  fs type；易失祖先 mount 不冒充持久卷。summary 行始终保留真实 mount target
  与 fs type（persistent=no 时不得误报 `mount=none`），warning 带
  `reason=volatile-mount|no-mount|not-writable`。其余 fs 类型沿用仓库既有
  约定：`CI_CACHE_ROOT` 是专用持久卷；该约定未实测远端持久性，性能与命中
  结论以真实 job 日志为准；
- `--with-deps` 的 apt 索引与 `.deb` 下载在持久缓存中复用，减少重复下载；
- apt 源临时改写与 scoped 代理（本地实测，同一 node:24-bookworm 容器、同一
  8,790,396-byte bookworm Packages.xz：直连 http 25 秒仅 401164 字节后
  timeout；直连 https 约 11KB/s，`curl --noproxy '*'` 20 秒仅 229145 字节；
  走 CI 大写代理的 https curl 2.8 秒完整下载。根因：workflow 只有大写
  `HTTP_PROXY`/`HTTPS_PROXY`，apt 不自动采用——容器内 `apt-config shell`
  下 `Acquire::*::Proxy` 为空）：脚本读取 `/etc/apt/sources.list` 与
  `/etc/apt/sources.list.d` 下适用 `.list`/`.sources`（seam
  `CI_PLAYWRIGHT_CACHE_APT_ETC`），仅把 `http://deb.debian.org` 与
  `http://security.debian.org` 改为同源 https，其余源字节不变；生成结构为
  RUNNER_TEMP 下主 one-line `.list`（仅来自 `sources.list`，缺失时为空文件）
  + sourceparts 目录（`.list`/`.sources` 以同名同扩展名逐文件改写）；两种
  apt 格式不混拼——真实 apt-get（node:24-bookworm `indextargets`）对拼接
  单文件报 `E: Type 'Types:' is not known`（exit 100），拆分布局解析 exit 0。
  apt 经 `Dir::Etc::sourcelist=<主文件>` + `Dir::Etc::sourceparts=<目录>`
  消费，原始 http 源不再生效；`/etc/apt` 原文件不动、源内容不打印；无可读
  source 文件时 wrapper 不加 source flags（与既有行为一致）。同时按标准
  环境变量注入 Debian 主机 scoped 代理：小写 `http_proxy`/`https_proxy`
  优先、回退大写 `HTTP_PROXY`/`HTTPS_PROXY`，https 可回退 http 配置；仅
  `Acquire::http(s)::Proxy::deb.debian.org|::security.debian.org`，不加全局
  Proxy（custom/internal mirror 保持自身路由）；`NO_PROXY`/`no_proxy` 匹配
  debian.org 或 `*` 时不注入；未配置代理时不加 proxy flags；代理值不打印、
  不入库。同源 HTTPS 替换与 scoped 代理共同解决 cold path；该收益在远端
  真实 CI 尚未实测，以真实 job 日志为准。

未实现预装浏览器镜像；本节不声称任何提速数字，性能结论以真实 job 日志为准。
缓存只复用依赖输入，不替代任何 GUI 测试、构建或浏览器验收。

## 本地复现 CI

Windows、macOS、Linux 开发者都应从仓库根目录使用 Docker 的 Linux container
模式运行下面同一条命令（支持 PowerShell、Bash 和 zsh）。它使用 CI 对应的
`node:22-bookworm` 镜像、uv 0.11.7、Python 3.12、相同预检和完整 pytest 命令；
源码只读挂载后复制到容器，既覆盖当前工作树，又不会用 Linux `.venv` 污染宿主：

```console
docker run --rm --mount "type=bind,src=${PWD},dst=/src,readonly" node:22-bookworm bash -lc 'set -euo pipefail; export GITHUB_RUN_ID="local-$$"; mkdir -p /work/repo; tar -C /src --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=__pycache__ --exclude=.agents/skills/forgejo-ops/.forgejo-token -cf - . | tar -C /work/repo -xf -; cd /work/repo; git init -q; git add -A; UV_VERSION=0.11.7; UV_BIN_DIR="/tmp/hyprial-uv-${GITHUB_RUN_ID}"; curl --fail --location --silent --show-error "https://uv.agentsmirror.com/github/astral-sh/uv/releases/download/${UV_VERSION}/uv-installer.sh" | env UV_UNMANAGED_INSTALL="${UV_BIN_DIR}" sh; export PATH="${UV_BIN_DIR}:${PATH}"; "${UV_BIN_DIR}/uv" --version; uv lock --check; BASETEMP="/tmp/hyprial-pytest-${GITHUB_RUN_ID}"; uname -a; uv run --locked --python 3.12 python -c "import multiprocessing, os, sys; print(\"python.executable={}\".format(sys.executable)); print(\"python.version={}\".format(sys.version)); print(\"multiprocessing.start_method={}\".format(multiprocessing.get_start_method())); print(\"TMPDIR={}\".format(os.environ.get(\"TMPDIR\", \"<unset>\")))"; echo "pytest.basetemp=${BASETEMP}"; uv run --locked --python 3.12 --extra test pytest tests/ -q --no-header --basetemp="/tmp/hyprial-pytest-${GITHUB_RUN_ID}"'
```

该命令不依赖宿主的 Python、uv、`/usr/bin` 布局、虚拟环境或 multiprocessing
默认启动方式。任何新增测试若只在某个宿主布局下成立，都不满足 CI 可移植性要求。

## 正式版本 CHANGELOG 硬门

`node scripts/ci/check-changelog.mjs --tag v<VERSION>` 要求 tag 与 `VERSION`
完全一致，且 `CHANGELOG.md` 有精确的 `## [<VERSION>]` 标题（可带日期），
节内至少有一条非空 bullet。代码块、HTML 注释、空节与版本前缀碰撞不能放行。
不传 `--tag` 时默认检查 `VERSION`；`--version` 可单独检查历史版本。

`release-tag.yml` 在创建 tag 前用 `--rev <目标 SHA>` 检查目标提交的两个文件，
而非 dev 工具 checkout。`github-publish.yml` 在构建快照和发布前检查所有 `v*`
发布（含 dry-run）。`wheel-release.yml` 仍只接收原有 track 事件，拒绝 `v*`；
另有构建前 `v*` 防御门，不新增正式 wheel 发布通道。
`internal` / `nightly` / `stable` 不要求正式版本节，触发与版本戳规则不变。

## GitHub 公网镜像(github-publish.yml)
把黑名单过滤后的快照推到 GitHub `Hyprial/hyprial` 并同步发布 tag。
触发、正式发布链和附件义务统一见 [发布规则](../../docs/guides/release-rules.md)。
- 快照 = 全树减去黑名单（Allen 2026-11-03；方案见
  [公网快照黑名单](../../docs/design/public-snapshot-denylist-2026-11-03.md)）。
  黑名单只存在于一处：`scripts/ci/github_publish.py` 的 `SNAPSHOT_DENYLIST`
  （逐项带理由），通配项在 `SNAPSHOT_EXCLUDE_GLOBS`，缺失即红的必需项在
  `SNAPSHOT_REQUIRED`。gitleaks 配置文件（`.gitleaks.toml` / `.gitleaksignore`）
  任何位置都不进快照，防止发布树自带配置关掉扫描闸。
- installability 闸直接在快照里 `uv build --wheel`：快照写入的 GUI 溯源戳
  `gui/gui-source.json`（`github_publish_policy/guistamp.py`）让 wheel 构建钩子在
  无 `.git` 的树里逐文件复核后打包 GUI；发布步骤仍从源 commit 重新抽取原始快照。
- 双扫描闸任一命中即红不推：gitleaks（钉版 8.30.1 + sha256 校验，日志只
  打 RuleID+文件）；内网标识闸：
  - 规则：git 主机名 / .ts.net / oc_ 客户端 id / CGNAT 段 / 员工邮箱 /
    mDNS 设备名 / 飞书 App ID，主机名与邮箱不区分大小写；
  - 私有标记：真实节点、设备与 handle 列在不公开的
    `github-publish-private-markers.txt`，命中不可豁免；
  - 文件路径同样扫描；
  - 允许清单 `github-publish-allowlist.txt` **按行**生效（路径 + 规则 +
    行内容 SHA-256 前 16 位 + 理由），失效条目判红，未豁免命中即红。
- `v*` 在目标仓不可变（已存在拒推）；三个可移动轨道仅 tag ref 可 force；快照树
  与上一版相同时不新建提交、只移 tag；提交者固定为 bot 身份。
- dry_run 走完全部闸与凭据 ls-remote 读数，⛔ 一律不 push。首次真实 push
  在 #520（tag 改名）合入后由 infra-op 单独 dispatch `dry_run=false` 执行。
- checkout 走内网 `${GITHUB_SERVER_URL}`；凭据 `GH_PUBLISH_TOKEN` 经
  GIT_ASKPASS，⛔ 不进 URL、不落日志。

历史：tsnet 边车曾有独立 tag 发布路径（边车二进制 GitHub Release），
已随 Tailcat 切换退役；新边车 hyprial-tailcat 随 wheel 分发，不再有独立
发布命名空间。

## 升级轨道 tag(三个可移动引用,task 25)
语义、写入权威和门禁只定义在 [发布规则](../../docs/guides/release-rules.md)。
10-26 原话与人工补齐记录保留在 [升级轨道历史](../../docs/guides/upgrade-tracks.md)。

实现细节（不另定义发布政策）：
共享 writer 复用现有 tag push 的 3 次有界重试（10s/30s），每次重试前回读；
stable lease 冲突响亮失败；nightly 并发冲突最多重新读取、计算 3 轮，不能收敛则失败。
绝不拿旧目标加新 lease 盲推。原位更新，不先删 tag。
随后 `check-release-tracks.py` 回读远端：stable 必须等于 v* commit，nightly 必须
等于它或为其后代，否则发布 job 失败。手工复核：
`uv run python scripts/ci/check-release-tracks.py 0.5.5.1`（输出含 internal，仅观察不改写）。
Actions-token push 不产生工作流事件，所以显式 dispatch 两轨公网同步；普通 track-tag
push 仍按既有行为触发 github-publish。轨道发布不进入 v* mover，不形成循环。
源仓检查不能替代公网快照、wheel 或实际安装验收。

nightly 晋升链（防半态契约，不得削弱）：`e2e-macos-isolated.yml` 与 `e2e-macos-credentialed.yml`
在**定时** run 全绿时各自把 `gate-e2e.json` / `gate-e2e-controlled.json`
推到 `ci/gate-status` 分支（记录 sha/runId/concludedAt，推送竞态用 rebase
解，绝不 force)；`release-nightly-promote.yml` 只在两个 marker **同 SHA 且时间戳
新鲜（≤4h)**时才产生该 SHA 的 E2E 候选 —— 单边绿、SHA 漂移、
marker 陈旧/缺失一律 NO-PROMOTE（健康结果，不是故障）。这些检查不变。
随后无论是否产生候选，都执行 stable 保底；旧候选不能回滚较新的 nightly/stable。

## macOS E2E
`e2e-macos-isolated.yml` 在 `macos-host` 上支持手动触发和每日定时执行。它先校验场景清单，再在
隔离的 `HYPRIAL_HOME`/`HARNESS_STATE_DIR` 下运行无需真实凭据的 runner：
E2E-001（Docker 模式）、E2E-002、E2E-003、E2E-005、E2E-007、E2E-009、E2E-010、
E2E-011（自动化部分）、E2E-015、E2E-016、E2E-017。

`e2e-macos-credentialed.yml`（同机，错峰定时 + 手动触发）承载需受控凭据/资源的场景：
E2E-004、E2E-006、E2E-008、E2E-012、E2E-013。凭据模型：测试 App
`allenwoods-hyprial-e2e` 的 `HYPRIAL_E2E_LARK_APP_ID`/`HYPRIAL_E2E_LARK_APP_SECRET`/
`HYPRIAL_E2E_LARK_CHAT_ID` 经 Forgejo secrets 注入（生产 App 为红线，Preflight 与
runner 双层校验，注入不符即响亮失败）；claude/codex/pi/lark-cli 登录态驻留在该
专用主机（v1，runbook 维护），缺失即 preflight 红。每个场景 step 记录 runner 退出
码（0=PASS/1=FAIL/2=BLOCKED/3=NOT-RUN），Gate verdict 聚合 step 要求全部 PASS
才绿——缺席、BLOCKED、NOT-RUN 都不是 PASS，也不得以 skip 掩盖。

E2E-009 的独立 runner（`contract/e2e/run-first-run-no-agent.py`）由 `e2e-macos-isolated.yml`
的隔离门驱动，在隔离 home 上证明
干净首次运行（`hyprial init`、重复 init、daemon 重启）不创建任何 agent、adapter、
route 或 pin，并证明退役的 `hyprial init onboarding` 命令与旧 step id 一律退出 2；
它不读取任何 App 凭据，也不使用真实模型。

明确不自动化（留门外，人值守）：E2E-001 的双 Tailnet 主机 [REAL ENV] 形态（需两台
物理主机）；E2E-011 步骤 14-15（平台要求真人扫码批准建 App + 租户管理员批 scope）。

## mock-model-harness-e2e（Linux 容器，原 hosted-agent-e2e）
模型是本地 FakeProvider（mock），其余全部是真的：hyprial CLI、隔离 daemon、已安装的
Pi / Codex / Claude harness。阶段 1 `run-real-harness-loopback.py` 让每个 harness
（pi、codex 的 headless/interactive，claude headless）各完成一轮真实对话；阶段 2
`run-group-conversation.py --harness pi` 跑 E2E-031 群聊场景（含 GC-11：两个被群消息唤醒的
headless Pi 经 mock 模型在群里各回一条）；阶段 3
`run-hosted-suite.py` 跑 AT01 hosted-registry、AT02 capability-ledger、AT03 hosted-start、AT04 hosted-upgrade。
2026-11-06 由 `hosted-agent-e2e.yml` 改名，status context 随之变为
`mock-model-harness-e2e / hosted`。

该 workflow 于 2026-10-04 从 `macos-host` 迁到 `ubuntu-latest` 的
`node:24-bookworm` 容器。这个套件使用合成的本地模型，不需要凭据，也不依赖 macOS API：
- Pi 从 npm 安装，版本固定为 `@earendil-works/pi-coding-agent@0.85.1`；
- 运行目录放在容器的 `/tmp`，证据以制品形式上传；
- 场景要求在测试前后比对「生产 daemon」的 pid 和 epoch，以此证明没有打扰同机的其他 daemon。容器里没有生产 daemon，所以先在 `$HOME/.hyprial` 启动一个隔离的替身 daemon 供比对，结束后停掉。

## 单测如何排除 E2E
**不需要 marker。** E2E 场景在 `contract/e2e/` 下,由独立 runner
(`_daemon_runner.py`)驱动、会起真实 daemon/容器 —— 它们**不在 pytest 收集范围**。
`pytest tests/` 只跑 `tests/`,天然不含 E2E。

## ci-light：skill-impact 类纯文本门禁的专用 host runner（2026-11-03）

Allen 2026-11-03：这些 0–1 s 的门禁在共享 `ubuntu-latest` 池排队 5–20 min
（实测 #2321 ~300 s、#2324 ~1060–1220 s），不再占用主 CI runner。以下 job 已切到
`runs-on: ci-light`（infra-ops 在 HQ 主机上注册的 host 模式 runner，capacity 12）：

* `gate-pr-declarations.yml` 的 `declaration`（body + diff，纯 bash/git）；
* `gate-pr-declarations.yml` 的 `impact`（body + diff + stdlib 脚本；
  `check_contract_impact.py` 同步去掉 `slots=` 保持宿主机 python3 3.9.6 可跑）；
* `gate-pr-declarations.yml` 的 `changelog`（stdlib 脚本 + 全量 checkout）；
* `gate-python.yml` 的 `pr-hygiene`（release/** 的 PR 仍走 `release-ci`，其余分支走 ci-light；
  job 名与 required context 名全部不变）。

runner 提供：宿主机直接执行的 bash/git/python3 3.9.6/node（Homebrew），自带 node22
runtime 执行 `uses:` JS action；无 Docker、无项目依赖——**任何需要 `uv sync`、测试套件
或构建的 job 不得迁入**。宿主机与容器池的两个环境差异由 pr-hygiene 的前两个步骤自适配
（其余三个 job 用同款 origin 探测）：容器专用名 `tailscale`（GITHUB_SERVER_URL）与
`host.docker.internal`（代理）在宿主机不解析，MagicDNS `forgejo`（http://forgejo:3000）
可达实例、`127.0.0.1:7896` 可达同一 mihomo 代理，且宿主机直连 pypi 会超时
（#2219 同款死 v6 路径）——解析默认值成功的池（release-ci）保持原样。改写值经
`env_sync` 同时进入当前 shell 与 `$GITHUB_ENV`：`$GITHUB_ENV` 只影响后续步骤，
同一步骤内的 remote add/fetch 依赖 in-step export（2026-11-05 评审阻断点，回归钉子
在 tests/tooling/ci/workflow/test_ci_light_host_adapter.py）。

## ⚠️ Runner 前提
Forgejo Actions 需要实例级 runner 向本仓提供 `ubuntu-latest` DinD 容器标签，并另有
`macos-host` runner 承担 E2E。单测红灯按 `node:22-bookworm` Linux 容器语义分析；
E2E 红灯按 macOS 宿主进程、文件锁和 Unix socket 语义分析，二者基线不能混用。

## 红灯处置原则
CI 不使用 `--ignore` 或 `-k` 排除环境敏感测试。resolver 测试在测试内创建受控
解释器/venv 与假可执行文件;Lark 跨进程测试显式使用 `spawn` context。若 daemon
shutdown 或并发压力测试仍失败,应按产品失败逐项报告并另卡修复,不能以宿主差异为由
skip/mask。

**环境契约（首跑教训，2026-10-09）**：四条规则会**自己再 fetch 一次** base 分支，
而 checkout 步骤的凭据是一个**临时 askpass**。首跑时 checkout 结束把它删掉了，
规则里的 `git fetch` 于是撞上凭据提示而失败（`could not read Username`，
task log 61522）：`pr-hygiene` 红，并如期把必需的 `tests / unit*` 一起带红。
现在按 hq 的 T3 裁定（规格由 infra-ops 出）：**凭据不得活过需要它的那一步** ——
checkout 的临时 askpass 仍只活它自己那一步（用完即删），三个取 fetch 的步骤在
**step 作用域**拿到 `PR_HYGIENE_GIT_TOKEN`，脚本再把它收窄成**每条 git 命令**的
`http.<server>/.extraHeader`（与 `gate-pr-declarations.yml` 同一套机制，不发明第二套认证），
并固定 `GIT_TERMINAL_PROMPT=0`（缺凭据要快速失败、不能挂在 /dev/tty 提示上）；
另有一条断言步报告 `leaked_in_later_step=none`，任何凭据超额存活即红；同一步还报告
`runner_injected_github_token=yes|no` 与 `runner_injected_forgejo_token=yes|no`
（M1：Forgejo 文档说两个名字都会注入每个 action step，此前只测了前者；两者都
**只报告不判红**——是否注入不是我们能改的，标准是自己不新增仓库级暴露）。
`resolve_base_tip` 只在**确无凭据**时降级为**比较 checkout 已抓到的 ref**，并打印
`base_source=checkout-ref` + `base_freshness=point-in-time-at-job-start` 以及一行人类可读
note（明说这是**时点性**读数）；**有凭据而 fetch 失败 ⇒ 红**（在那里回落等于把 rule 1
悄悄做软）；两者都拿不到也红（compare, never assume）。四条门现在**绿跑也打印**
`pr-hygiene measured: gate=… base=<remote>/<ref>@<40hex> head=@<40hex> …`
（rule 2 另打印 `tree=@<40hex>`），所以「门验的是哪个 ref」可以直接从日志回答。
