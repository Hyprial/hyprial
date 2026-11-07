# AGENTS.md — Harness Bridge (hyprial)

面向 AI coding agent 的仓库工作说明。人类贡献者指南见 [CONTRIBUTING.md](CONTRIBUTING.md)；
产品架构见 [README.md](README.md) 与 [docs/design/design.md](docs/design/design.md)。
本文件是唯一事实源；`CLAUDE.md` 仅以 `@AGENTS.md` 导入本文件。

## 项目一句话

hyprial 是面向 AI agent 与其 harness 的去中心 durable 通信层：daemon 持有 durable inbox
与路由状态（消息与恢复的权威边界），connector 接入 Claude Code / Codex / Pi 等 harness，
adapter 接入 Lark 等外部平台，PAC 负责声明式派单与跟踪（不替 worker 执行）。

## 环境与命令

- Python 3.12+，依赖与命令一律由 **uv** 管理：`uv sync --extra test`、`uv run --with libcst==1.9.0 pytest tests/`、`uv run ruff check .`。
  全套包含 `tests/tooling/refactor`；`--with` 只叠加工具项目已钉定的 LibCST，不修改产品依赖或根锁。
  普通产品焦点测试无需该叠加；工具独立命令见 `scripts/refactor/README.md`。
- **禁止裸跑 `python`/`python3`**（会命中系统解释器、丢锁定依赖）；`.agents/hooks/enforce-uv.py`
  已在 Claude Code（`.claude/settings.json`）与 Codex（用户级配置）强制改写。
- 跨实现契约测试：`./contract/run-all.sh --isolated-only`。
- 本批退场计划将旧独立 GUI（`src/gui`）与 Tauri 外壳（`app`）移出运行路径；组件化
  网页 UI 以根 `gui/` 为唯一主线，由 UI 任务独立 PR 提供。CLI 只公开裸
  `hyprial gui` 和 `hyprial gui dev`；`start|status|stop|upgrade` 返回 `INVALID_ARGUMENT`。
  安装 wheel 的裸 `hyprial gui` 校验内置 `hyprial/_gui/release.json`（完整 40 位
  sourceCommit、trusted sourceRemote、包/GUI 版本、逐文件 SHA-256）并由仅依赖 Python
  的 loopback 静态服务提供构建资源，不需要源码、Node、npm 或 Git；源码 checkout 仍走
  npm 构建/启动。`hyprial gui dev` 的优先级是 `--gui-home` > `HYPRIAL_DEV_GUI_HOME` >
  可验证源 checkout > wheel 清单精确 `sourceCommit`；安装态只从清单可信 remote 获取
  精确 SHA（不取 latest/任意分支），使用受管 bare cache 和精确 SHA 稀疏工作树，保留
  dirty/untracked 文件，HEAD 不符即拒绝，不自动 fetch/rebase，并打印真实 path/HEAD/
  branch/remote/baseline/dirty 与显式更新提示。`HYPRIAL_GUI_HOME` 只用于普通 GUI，
  dev 不用它作为源码来源；`HYPRIAL_DEV_GUI_HOME` 与它相互独立、不互相回退。
  源码模式启动独立 Node 占位服务，安装静态服务与所有源码模式业务均明确返回
  `not implemented: hyprial backend not wired`；本批不接真实 daemon、不恢复旧 Python
  Web/backend，也不代表打包或产品验收。操作见 `docs/guides/gui-dev-mode.md`；退场入口
  与恢复责任见 `docs/design/gui-retirement-restore-ledger-2026-10-27.md`。
- 修改产品或测试结构时先执行 `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`
  与 `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`；它们也是早期 CI 硬门。

## 已知问题

- 开发或修 bug 前，先查 [docs/reference/known-problems.md](docs/reference/known-problems.md)。已登记且决定“不修 / 待测”的问题不重复投入；并非所有问题都需要修。
- 新发现但决定暂不修的问题，登记进该文档，写明现象、影响、规避、决定、依据与负责人；状态变化时更新原条目，不删除。

## 测试纪律

- 先跑直接覆盖改动的焦点测试，再跑全量 `uv run --with libcst==1.9.0 pytest tests/`；交付前 `uv run ruff check .`。
  主测试套件明确限定 `tests/`；`docs/notes` 与 evidence 中的历史复现不属于主 collector，
  不为使默认递归发现通过而改写 WORM 或恢复已经退役的产品 import。
- 行为变化必须有先失败再通过的测试；真实环境场景明确报告 PASS / FAIL / BLOCKED，不用 skip 冒充结论。
- 测试产生的 home / socket / 端口 / 缓存必须与日常环境隔离；**绝不读取、改写或清理生产
  daemon、Lark App、凭据与用户级配置**。
- GUI 启动必须显式指定 backend driver 与可信 principal；mock 只用于明确选择的隔离开发/测试。
  GUI/desktop 不创建第二份 durable inbox、业务会话或 turn 权威；浏览器请求不能自报可信身份。
  unit/build 不能替代真实 backend、浏览器或完整功能等价验收，未实测项报告 UNKNOWN/BLOCKED。
- 前端新增或迁移组件遵循 [前端组件规范与 shadcn/ui 接入](docs/guides/frontend-components.md)。
  以根 `gui/` 的 TypeScript primitives 和 `--ui-*` token 为基础；接入状态以源码及组件目录为准，
  当前 fixture/HTTP 501 占位边界保持有效，不恢复旧 `src/gui/` 或 `app/`。

## GUI 单套界面约束

修改 GUI 前必须阅读 [单套 GUI 规则](gui/docs/ui-development-standard.zh-CN.md#single-gui) 和
[共享页面入口映射](gui/docs/shared-ui-entry.zh-CN.md)。预览、正式 Web 和原生壳复用同一展示实现；
只允许数据源、能力适配器、控制器及开发工具栏不同。不得复制 preview/live 页面或伪造成功。
按 [UI 交接模板](gui/docs/ui-change-handoff-template.zh-CN.md) 提交共享组件路径、双入口证据和未测范围。
本节不定义后端接口，也不改变各业务能力的接线状态。

## 提交与 PR

- 从目标分支新 head 建短分支；分支名用 `<type>/<slug>`（type 与提交类型同一套：`feat/fix/docs/test/refactor/ci/design/intg…`）；
  conventional commit（`feat/fix/docs/test/refactor: …`），每个提交可读、可测试。
  `.agents/hooks/branch-name-hint.py` 会在建分支时提醒不合规的名字（只提醒、不阻断）。
- PR 正文写动机、改动、验证命令与实测结果；未测项显式写 UNKNOWN 或 BLOCKED。
- 改变已发布字段语义（JSON 键、诊断字段、事件名、错误码）须同步 `contract/`，否则 PR 正文写
  `Contract-Impact: <放行理由>`（CI 门：`scripts/verify/check_contract_impact.py`）。
- 改变 agent 操作方式须说明 Skill-Impact 并同步相关 skill。不绕分支保护。

## 失联韧性（第一原则）

本仓库的协作经过 agent 网络（人 → Lark → 协调者 → worker），**消息通道本身就是我们维护的产品**。
因此：任何关键信息——目标、决策、现场、证据——不得只存在于消息通道或某个 agent 的记忆里，
必须落仓库。长期目标与发版义务见 [ROADMAP.md](ROADMAP.md)（每条自含触发条件、执行命令、
红线与实测记录；失联时先读它）。验证三态纪律：BLOCKED 不是 PASS，skip 不是结论。

## 仓库布局纪律

- 产品源码根为 `src/hyprial/`；目标布局的共享网页 UI 为根 `gui/`，原生壳为
  `clients/native/`，客户端构建与载荷归 `packaging/clients/`。旧 `src/gui/`、`app/src/`
  与 `app/src-tauri/` 不恢复、不继续扩展。文档归 `docs/`（design/guides/reference/domains/evidence 五桶），
  证据与复现材料归 `docs/notes/`（WORM，只追加）；脚本归 `scripts/`（ci/build/verify/archived）。
- 根目录不新增 md 文件（白名单：README、ROADMAP、CHANGELOG、CONTRIBUTING、LICENSE、AGENTS.md、CLAUDE.md、GLOSSARY.md）。
- Python 按 `kernel/identity/daemon/biz/shell` 五个深模块组织，每域由 `interface.py` 定义公共面，
  包入口导出该面，实现隐藏在 `impl/`。跨域只消费公共面，不导入另一域 `impl`，也不复制其权威逻辑；
  依赖按 kernel → identity → daemon → biz → shell 分层，上层消费下层。
  根 `cli.py`、`gui_build_hook.py` 是薄入口；`gui_build_hook.py` 负责 tailcat 与 wheel
  内置 GUI 资源/清单载荷，旧 GUI 生命周期已临时退场。
- 硬 CI 限制：产品源码根及 `tests/` 中，每个含代码目录最多 **7 个直接文件**（计入同目录文档/数据），
  每个代码文件最多 **1000 行**；测试 helper 与 Go fixture 同样受限，不靠 per-file 豁免或改名隐藏代码。
  第三方/生成物边界以结构门为准。
  活动 `scripts/`、`contract/`、`sidecar/` 另外执行 1000 行门；这些独立入口集合不按产品模块计目录文件数。
  `sidecar/` 中含 `.go` 源码的目录是 Go 包，同样执行 7 个直接文件门（包括同目录测试/文档/数据）；
  边车的独立 Python/shell E2E runner 集合仍只接受行数门，不重复统计 Go 文件。
  仅精确的历史 `scripts/archived/` 排除，不能把活动代码移入归档来逃避检查；WORM 材料保持原字节。
  GUI 模块跨边界只导入各自 `index.mjs`，职责与命令见 GUI skill。
- CLI 输出只经 `shell/impl/cli/output`：命令返回 `CliResult(data, render)`（流式为 `CliStream`），
  不自己 `print`/`typer.echo`/写 stdout；`--json` 时 stdout 恰好一个 JSON 对象且必带 `ok`，`ok:false`
  退出 1；进度、警告、确认走 stderr（`progress`/`warn`/`notice`/`confirm`）。kernel/identity/daemon/biz
  不导入 typer、click、rich。门禁见 `tests/shell/cli/output/test_output_discipline.py`，设计见
  `docs/design/cli-output-unification-2026-10-04.md`。
  方案与 C0 证据见 `docs/design/refactor-plan-deep-modules-2026-10-01.md` 与
  `docs/design/refactor-c0-and-tooling-2026-10-01.md`。

## 代码注释纪律（Allen 2026-10-17）

「后续不要在代码中添加注释，容易导致决策被之前的注释影响……注释仅限于文件头，列出相关的决策过程，而不是直接将注释写在代码中」
「可以先加 CI 提醒删去注释，后续在开发过程中逐渐消除注释，不急一次性完成。docstring 注释保留，覆盖范围是所有代码」

- 禁止新增行内注释；决策记录写进文件头决策块（shebang/encoding 之后、第一行代码之前的连续注释块）。
- docstring 保留；`# noqa`、`# type:`、`# pragma`、`// eslint-disable` 等工具指令不算注释。
- 存量注释随开发逐步消除，不要求一次性完成。
- 提醒：`scripts/ci/check_inline_comments.py` 只看 PR diff 的新增行，默认不阻断；`--strict` 是唯一的升级开关（CI step 里加 `--strict` 并去掉 `continue-on-error`）。测试见 `tests/tooling/ci/hygiene/test_inline_comments.py`。

## Skills 与 hooks

- `.agents/skills/hyprial-e2e/`：daemon/provider/Lark/分发链路变更的 E2E 场景选择（发版必看，另见 ROADMAP.md）。
- `.agents/skills/hyprial-workflow/`：PAC 派单工作流。
- `.agents/skills/gui-workflow/`：历史 GUI/desktop 构建与验证记录；当前临时退场，不描述本版可用能力。
- `.agents/skills/forgejo-ops/`：Forgejo 认证、SSH transport、PR 与 CI 操作。
- `.agents/hooks/`：uv 强制改写 hook，Claude / Codex 双 harness 共用（见该目录 README）。

## 安全

凭据、生产状态、本机私有配置不入库；安全问题走维护者私有渠道，只附最小脱敏证据。
