# AGENTS.md — Harness Bridge (hyprial)

面向 AI coding agent 的仓库工作说明。人类贡献者指南见 [CONTRIBUTING.md](CONTRIBUTING.md)；
产品架构见 [README.md](README.md) 与 [docs/design/design.md](docs/design/design.md)。
本文件是唯一事实源；`CLAUDE.md` 仅以 `@AGENTS.md` 导入本文件。
术语以 [GLOSSARY.md](GLOSSARY.md) 为准（写代码、文档、提交信息、契约前先查；新增公开术语先进表）；
组织与会话模型（presence/link → cell → agent → thread → conversation → department）见
[docs/design/cell-thread-conversation-model-2026-11-03.md](docs/design/cell-thread-conversation-model-2026-11-03.md)。

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
- 旧独立 GUI（`src/gui`）与旧 Tauri 外壳（`app`）已退场，仓库里不再有；组件化
  网页 UI 以根 `gui/` 为唯一主线。CLI 只公开裸
  `hyprial gui`（可加 `--with-dev-tools`、`--live-source`）和 `hyprial gui dev`（即 `hyprial gui --with-dev-tools --live-source`，同一条启动路径，不另有实现）；`start|status|stop|upgrade` 返回 `INVALID_ARGUMENT`。
  安装 wheel 的裸 `hyprial gui` 校验内置 `hyprial/_gui/release.json`（完整 40 位
  sourceCommit、trusted sourceRemote、包/GUI 版本、逐文件 SHA-256）并由仅依赖 Python
  的 Python 会话 host（`hyprial.shell.impl.gui.host`）提供构建资源，并经 admission 通过 host 进程内的
  `request_http` 接入真实 daemon，不需要源码、Node、npm 或 Git；wheel 未设
  `HYPRIAL_GUI_BACKEND_DRIVER` 时默认 `daemon`，`contract-mock` 返回
  `GUI_MOCK_UNAVAILABLE`；运行时单实例锁走 `kernel.ProcessFileLock`、`--stdin-stop` 按能力投递停止，
  不因平台缺 `fcntl`/`os.getuid`/`signal.pthread_kill` 返回 `GUI_RUNTIME_UNAVAILABLE`（owner 2026-11-21 解除
  「Windows 不排期」，Windows 真机验收见 KP-080）；源码 checkout 先 `npm run build`，再由同一个 Python 会话 host 提供 `gui/dist`（不再有 Node host，`npm start` 已删除）。live source（`--live-source` 与 `hyprial gui dev`）的源码优先级是 `--gui-home` > `HYPRIAL_DEV_GUI_HOME` >
  可验证源 checkout > wheel 清单精确 `sourceCommit`；安装态（清单 remote 非公开仓库的内部/本地构建）只从清单可信 remote 获取
  精确 SHA（不取 latest/任意分支），使用受管 bare cache 和精确 SHA 稀疏工作树，保留
  dirty/untracked 文件，HEAD 不符即拒绝，不自动 fetch/rebase，并打印真实 path/HEAD/
  branch/remote/baseline/dirty 与显式更新提示；清单 remote 为公开仓库的发布 wheel 不走该缓存，无显式源时以 `INVALID_ARGUMENT` 拒绝并要求源 checkout 或 `--gui-home`。源码 checkout 下 live source
  直接使用该 checkout 自身的 `gui/`，不创建工作树、不安装依赖。`--gui-home` 只属于
  live source；不带 `--live-source` 的 `hyprial gui` 只有 wheel 内置 `_gui` 与源码 checkout 两个目标，
  传 `--gui-home` 返回 `INVALID_ARGUMENT`，调用环境中设置 `HYPRIAL_GUI_HOME` 返回
  `GUI_CONFIG_CONFLICT`。`HYPRIAL_GUI_HOME` 只是 launcher 交给自身子进程的内部变量，live source
  忽略调用环境中的该值；不带 `--live-source` 时 `HYPRIAL_DEV_GUI_HOME` 不起作用。
  源码模式与 wheel 由同一个 Python 会话 host 承载会话；`--live-source` 把该 host 放在公开端口，
  Vite dev server 只经 loopback 随机端口出资源与 HMR，不持有任何 `HYPRIAL_GUI_*` 变量、grant 或 bearer
  （host 以 `--live-upstream` 反向代理 HTTP 与 HMR WebSocket，WebSocket 须带同源 Origin）。
  开发工具层与 API 文档只在 `--with-dev-tools` 下由 host 提供（`dev-tools/*`、`/docs`、`/docs-assets/*`、
  `POST /daemon/session/attach`、`GET /daemon/openapi.json`）；默认启动与原生壳不传该开关，这些路径一律 404，
  会话体没有 `devTools`，live source 下也由 host 自己回 404、不转发给 dev server。发布 wheel 接受
  `--with-dev-tools`（工具随 wheel 发布、默认不加载不提供），拒绝 `--live-source` 与 `hyprial gui dev`。
  旧的 bearer Swagger 页面已删除，`request_http` 不再提供 `/docs`；API 文档只由 session host 在
  `--with-dev-tools` 下提供。`hyprial gui --json` 的启动记录以
  `dev_tools`、`api_docs`、`live_source` 报告实际状态。
  `HYPRIAL_GUI_BACKEND_DRIVER` 未设时默认 `daemon`，`contract-mock` 须显式选择；driver 由 launcher 命名，
  浏览器请求不能选择；`hyprial gui --json` 的 `backend_driver` 报告实际 driver。不恢复旧 Python
  Web/backend，也不代表打包或产品验收。操作见 `docs/guides/gui-dev-mode.md`；退场入口
  与恢复责任见 `docs/design/gui-retirement-restore-ledger-2026-10-27.md`。
- daemon 先行（owner 2026-11-15；`init` 不登录、登录为 daemon 操作为 2026-11-18）：daemon 是唯一常驻后端。`hyprial gui` 在 home 的每种状态下都先确保
  daemon（前提：home 目录已存在且为私有，否则先报 `GUI_HOME_NOT_INITIALIZED` / `GUI_HOME_UNSAFE`），无 owner 的 home 也一样（启动 bootstrap daemon）；关闭 GUI 不停 daemon；principal 变化（登录、登出）
  在原进程内换绑，GUI 进程与它的 origin 不变。`hyprial init` 从不登录；无 owner 的 home 上的 `hyprial login`
  是 daemon 的一个操作，CLI 必要时建 home、拉起 bootstrap daemon，其余只负责呈现（显示 URL 与用户码、打开浏览器、等待）；`--no-daemon` 已移除；一个 home
  永久绑定一个 uid（`settings.homeAccount`），同一账号的重新登录由 serving daemon 的 `auth.relogin` 驱动，
  `--switch-account` 已删除，换账号即换 home（见 `docs/guides/migration-account-binding.md`；KP-112 已关闭）。原生壳（`clients/native/`、`packaging/clients/`，2026-11-15 起重建）
  运行 `hyprial gui --no-open --json --port 0 --stdin-stop`。
- 单一 Python host 进程（#2822：步骤 1–4 已合入 #2985、#2995、#2996）：daemon driver 下由会话 host
  在自己的进程内提供 `request_http`，`hyprial gui` 只起一个 Python 子进程、一个 loopback 监听端口；launcher 不写
  `server.json` 与 `frontend-authority.json`，不给 host 传 bearer。启动记录的 `backend_pid` 为 `null`、
  `backend_endpoint` 为空串（`record_version` 仍为 1）。`contract-mock` driver 仍另起一个 Node 子进程。文件式 authority 读取与
  `HYPRIAL_GUI_AUTHORITY_PATH` 已删除（owner 2026-11-20 批准）：host 的 authority 只来自进程内的 `on_rebound` 交接
  （daemon driver）或启动时固定的 endpoint+token（contract-mock 或一致性测试宿主），
  `python -m hyprial.shell.impl.request_http serve` 仍存在，但只服务 `local-operator` 配置；给它 `local-gui` 配置时以 `CONFIG_INVALID` 退出码 2 拒绝（GUI 主体只由会话 host 在自己的进程内提供）。
- 修改产品或测试结构时先执行 `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`
  与 `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`；它们也是早期 CI 硬门。

## 已知问题

- 开发或修 bug 前，先查 [docs/reference/known-problems.md](docs/reference/known-problems.md)。已登记且决定“不修 / 待测”的问题不重复投入；并非所有问题都需要修。
- 新发现但决定暂不修的问题，登记进该文档，写明现象、影响、规避、决定、依据与负责人；状态变化时更新原条目，不删除。

## 测试纪律

- 先跑直接覆盖改动的焦点测试（任何代码变更都适用）。只有 `src/hyprial/` 变化才追加全量
  `uv run --with libcst==1.9.0 pytest tests/` 并在交付前跑 `uv run ruff check .`；
  `tests/`、`scripts/`、`pyproject.toml`、`uv.lock` 与 GUI 变化不单独触发全量 pytest/Ruff
  （显式手动 full validation，如 `workflow_dispatch`、main 与已批准 deep-refactor 全量路径，例外）。
  GUI/TypeScript 或普通文档变更不要求全量 pytest 或 Ruff，
  但适用的 GUI 测试、CI 策略门（Node `ci-scope`/`ci-plan` 测试）与结构检查不取消。
  主测试套件明确限定 `tests/`；`docs/notes` 与 evidence 中的历史复现不属于主 collector，
  不为使默认递归发现通过而改写 WORM 或恢复已经退役的产品 import。
- 行为变化必须有先失败再通过的测试；真实环境场景明确报告 PASS / FAIL / BLOCKED，不用 skip 冒充结论。
- 测试产生的 home / socket / 端口 / 缓存必须与日常环境隔离；**绝不读取、改写或清理生产
  daemon、Lark App、凭据与用户级配置**。
- GUI 的 backend driver 由 launcher 决定（安装态与源码态未设时默认 `daemon`，`contract-mock` 须显式选择；浏览器不能选），可信 principal 由 launcher 从已提交 owner 派生；mock 只用于明确选择的隔离开发/测试。
  GUI/desktop 不创建第二份 durable inbox、业务会话或 turn 权威；浏览器请求不能自报可信身份。
  unit/build 不能替代真实 backend、浏览器或完整功能等价验收，未实测项报告 UNKNOWN/BLOCKED。
- 前端新增或迁移组件遵循 [前端组件规范与 shadcn/ui 接入](docs/guides/frontend-components.md)。
  以根 `gui/` 的 TypeScript primitives 和 `--ui-*` token 为基础；接入状态以源码及组件目录为准，
  安装态 `hyprial gui` 已由 Python 会话 host 在自己的进程内经 `request_http` 接真实 daemon，不再是占位；
  源码态以 `docs/guides/gui-dev-mode.md` 为准，不恢复旧 `src/gui/` 或 `app/`。

## GUI 单套界面约束

修改 GUI 前必须阅读 [单套 GUI 规则](gui/docs/ui-development-standard.zh-CN.md#single-gui) 和
[共享页面入口映射](gui/docs/shared-ui-entry.zh-CN.md)。预览、正式 Web 和原生壳复用同一展示实现；
只允许数据源、能力适配器、控制器及开发工具栏不同。不得复制 preview/live 页面或伪造成功。
按 [UI 交接模板](gui/docs/ui-change-handoff-template.zh-CN.md) 提交共享组件路径、双入口证据和未测范围。
本节不定义后端接口，也不改变各业务能力的接线状态。

GUI 的数据请求只有 daemon request 一条路径（owner 2026-11-14）。`gui/src` 中只有传输模块可以引用 `fetch` 等网络原语、
构造 `HttpTransport` 或书写请求路径；`gui/tests`、`gui/scripts` 与 Vite 配置不得另起提供业务数据的路由、HTTP 服务，
浏览器测试不得再新增 `route.fulfill` 伪造应答，数据宿主有两个：Python 会话 host（`src/hyprial/shell/impl/gui/host`，Node host 已于 2026-11-16 删除）与 Contract Mock 引擎 `gui/contract-mock`。规则与例外登记方式见
[数据请求只有一条路径](gui/docs/ui-development-standard.zh-CN.md#single-request-path)；门禁是
`scripts/ci/check_gui_request_paths.mjs`，其 `allowlist`（带理由的例外）与 `pending`（待删存量）都只减不增，
与基线分支比较，在同一 PR 里新增违规的同时新增清单行会失败；扫描器配置 `scripts/ci/gui-request-paths-scanner.json` 同样与基线分支比较，只许扩大、不许缩小。
mock 数据只有一个来源（owner 2026-11-15）：例子只在契约 `contract/daemon-request/openapi.json`，引擎只有 `gui/contract-mock`
（Node 进程与浏览器内两种形态，`node gui/contract-mock/serve.ts`；Python 不再提供 mock 服务）。`gui/src` 不得手写按方法名分派的应答、
不得有生成表以外的数据文件、`gui/src/dev` 手写 fixture 登记行数上限；门禁是 `scripts/ci/check_gui_mock_source.mjs`
（清单 `scripts/ci/gui-mock-source-ledger.json`，同样只减不增并与基线分支比较；自测 `check_gui_mock_source.selftest.mjs` 只在 `gate-gui` 运行）。
规则见 [数据请求只有一条路径](gui/docs/ui-development-standard.zh-CN.md#single-request-path)。

## 提交与 PR

- Git flow 以 [`.github/git-flow.json`](.github/git-flow.json) 为机器可读规定；所有完成的
  修改先进入 dev，release 分支仅作回流 dev 的发布准备。CI 按完整文件差异选择套件。
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

- 产品源码根为 `src/hyprial/`；共享网页 UI 为根 `gui/`。最小原生壳位于
  `clients/native/`，只负责窗口、单实例和监督 `hyprial gui` 子进程；不持有 GUI/backend/session/inbox
  权威。旧 `src/gui/`、`app/src/` 与 `app/src-tauri/` 不恢复、不继续扩展。文档归 `docs/`
  （design/guides/reference/domains/evidence 五桶），
  证据与复现材料归 `docs/notes/`（WORM，只追加）；脚本归 `scripts/`（ci/build/verify/archived）。
- 根目录不新增 md 文件（白名单：README、ROADMAP、CHANGELOG、CONTRIBUTING、LICENSE、AGENTS.md、CLAUDE.md、GLOSSARY.md）。
- Python 按 `kernel/identity/daemon/biz/shell` 五个深模块组织，每域由 `interface.py` 定义公共面，
  包入口导出该面，实现隐藏在 `impl/`。跨域只消费公共面，不导入另一域 `impl`，也不复制其权威逻辑；
  依赖按 kernel → identity → daemon → biz → shell 分层，上层消费下层。
  除五个深模块外，`src/hyprial/error/` 存放各层共享的线路错误词汇（现为错误码，异常类型后续迁入），
  它不导入任何产品模块，可被所有层直接导入。
  根 `cli.py`、`gui_build_hook.py` 是薄入口；`gui_build_hook.py` 负责 tailcat 与 wheel
  内置 GUI 资源/清单载荷，旧 GUI 生命周期已删除。
- 本机 IPC（unix socket 与 Windows named pipe）一律走 kernel 封装
  `hyprial.kernel.listen_local_ipc` / `connect_local_ipc`；平台层（`kernel/impl/platform/`）之外
  不得出现 `AF_UNIX`（任何写法，含 `hasattr`/`getattr` 探测）、asyncio/socketserver 的 unix 端点
  或直接的 named pipe 调用（`scripts/ci/check_windows_portability.py` W020，零基线）。
- 硬 CI 限制：产品源码根及 `tests/` 中，每个含代码目录最多 **7 个直接文件**（计入同目录文档/数据），
  每个代码文件最多 **1000 行**；测试 helper 与 Go fixture 同样受限，不靠 per-file 豁免或改名隐藏代码。
  第三方/生成物边界以结构门为准。
  活动 `scripts/`、`contract/`、`sidecar/` 另外执行 1000 行门；这些独立入口集合不按产品模块计目录文件数。
  `sidecar/` 中含 `.go` 源码的目录是 Go 包，同样执行 7 个直接文件门（包括同目录测试/文档/数据）；
  边车的独立 Python/shell E2E runner 集合仍只接受行数门，不重复统计 Go 文件。
  仅精确的历史 `scripts/archived/` 排除，不能把活动代码移入归档来逃避检查；WORM 材料保持原字节。
- 根 `gui/` 的 TypeScript 业务模块统一由 `index.ts` 定义显式公共面，实现放在 `impl/`；
  跨 feature 只消费公共入口，禁止穿透 `impl/`。
  `npm --prefix gui run check:module-boundaries` 检查模块结构、跨模块/分层依赖及 dev/test 反向依赖；
  当前例外精确登记于 `gui/config/module-boundary-baseline.json`，只减不增。结构目标与分工见
  `docs/design/gui-deep-module-structure-2026-11-08.md`。
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
- `.agents/skills/gui-workflow/`：GUI 业务线分工与工作方式、当前启动与原生壳的指针。
- `.agents/skills/forgejo-ops/`：Forgejo 认证、SSH transport、PR 与 CI 操作。
- `.agents/hooks/`：uv 强制改写 hook，Claude / Codex 双 harness 共用（见该目录 README）。

## 安全

凭据、生产状态、本机私有配置不入库；安全问题走维护者私有渠道，只附最小脱敏证据。
