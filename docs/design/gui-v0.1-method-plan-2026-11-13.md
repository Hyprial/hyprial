# GUI v0.1 用户旅程与方法对账

这是当前唯一的功能、方法和接线对账；执行顺序与验收条件见 [TODO](gui-v0.1-lifecycle-todo.md)，
术语见根 [GLOSSARY](../../GLOSSARY.md)，启动方式见 [开发指南](../guides/gui-dev-mode.md)。

## 当前候选交付范围（2026-11-14）

最新公告将本分支冻结为合入候选：只修冲突和本线新增CI失败，不再实施下文未完成方法；#2738不在本轮处理。#2755第3、4.2、5节不改变当前daemon request权威边界。现存reference/stub默认客户端路径、手写开发fixture及静态不可用文案留给后续收拢PR，本轮只登记、不新增；`clients/native`、`packaging/clients`相对当前dev无改动。完整CI的每项失败须有当前dev/KP/新增归属，必需检查不绕过，满足后再给用户手测。

用户已收敛为先固定一个能启动的组合：Pi 原生 Auth 增量已合并 dev `dab89f8c6`，测试修复后为 `b685ffc2c`；PR再次冲突后继续对齐 dev `f04d40b3a` 的旧迁移链路退役。登录/初始化 → `agent.create` → `agent.provider.login`（两文件）→ `agent.provider.auth.list` → `agent.start`（显式模型/cwd/approve）→ 读取运行状态 → 导航 Chat，全部沿共享 GUI → GeneratedClient → HTTP → MethodSpec → daemon request → daemon。不调用 CLI 代替业务请求。
真实浏览器/HTTP/daemon/Pi 已在合成 Auth、受控模型下完成启动和导航；真实厂商认证与基础回复未通过，不能把导航视为聊天成功。Key（KP-030）、其他启动组合、删除及完整聊天继续保留在本文和 TODO，后续补齐，不再作为本次候选交付前提。固定 SHA 独立审核和回归结果仍须结算。

## 核查基线与判定

### 当前 dev 合并与启动核对（2026-11-14）

最新对齐目标为 `dev@f04d40b3abb014636b3e4daea01bf8fb6c73a876`。#2723退役旧agent move/transfer/P2实现，两个修改/删除冲突均保留dev删除；本线曾给P2 receive加入的pin端口消费者随之删除，Agent pin权威与删除预约仍保留。九个旧transfer方法属于legacy router退役，本次没有重新声明或提供别名。重新导入目录：b685为153声明/124 HTTP，dev为138/102，合并树为153/124；本线每个声明哈希和OpenAPI不变，dev声明零丢失。源码中旧P2引用已消除；运行、启动器及浏览器结果须以此次合并的新证据为准。

合并定向后端497 PASS，CI计划/收集134 PASS；方法登记、请求契约、结构/边界、daemon架构、术语、生成客户端和TypeScript检查通过。包含隔离真实HTTP登录/初始化/退出生命周期，未重跑外部厂商认证、完整浏览器旅程或全量Python。证据 `/private/tmp/gmf-y294ryjw`；固定新SHA独立审核与用户手测单独结算。

合入门槛复核纠正了此前失败归属：post-install 13个分支检查在当前dev全通过，本线漏同步3个新增预加载模块的观测清单；原生成器更新后22项fence/负例通过，函数集合和场景计数不变。dev删除P2后，本线两项测试残留旧导入；纯P2装配用例随产品退役，facade补偿用例改为直接调用保留的pin权威，39项pin/退役测试通过。6项Linux uv缓存用例在两个固定树同样因Darwin拒绝而失败。干净候选Ruff/CLI inventory/CI策略/契约差异/网络/等待/Windows检查通过，证据 `/private/tmp/gci-f8rl8g9y`；以上焦点不替代修后完整CI。

再次读取#2755 head `7a3db37bc`：设计新增“删除原生壳后最简重建”和所有GUI API统一daemon request等决策，仍未实现新host或参数。此处保留当前活动入口，后续按该PR实施结果迁移，不恢复已退役Web通道，也不自行重复实现新host。

公开集成头e4828e768合入dev cecde87f6；共同基线64551e355。按每个MethodSpec比较三方契约，上游仅新增 `teleport.run`（非HTTP），未退役或改变本线方法。本线15个新增、9个变更声明均保留；合并目录153个声明/124个HTTP方法，与运行时导出逐字一致。正式业务链仍是共享GUI → GeneratedClient → HTTP → MethodSpec → daemon request → daemon。teleport新增的服务装配/关闭顺序需要实际回归，目录一致不等于运行通过。

已实读 [#2755](https://code.hyprial.com/HyprialOS/hyprial/pulls/2755) 的 `docs/design/gui-single-launch-2026-11-14.md`：当前只是设计，没有新增参数或替换宿主。目标是统一Python同源宿主，Vite只供开发资源，原生只提供窗口/宿主能力。当前源码手测应在固定checkout目录，显式设置隔离HOME/HYPRIAL_HOME/HARNESS_STATE_DIR和 `HYPRIAL_GUI_BACKEND_DRIVER=daemon` 后运行 `uv run hyprial gui --port <隔离端口> --json`；**不传普通GUI的 `--gui-home`**，它会选静态资源路径。不能使用尚未实现的 `--with-dev-tools` / `--live-source`。完整环境与重启说明沿用 [开发指南](../guides/gui-dev-mode.md)，实际服务地址和固定SHA在验收时记录。

此次不恢复已删除的Web auth/setup/live、reference产品入口或另一份登录流程；保留四步共享引导及现有原生生命周期。合并后正式启动、隔离浏览器/Pi与用户厂商认证需重新验证，原生安装包仍由打包线验收。

最终同步到dev dab89f8c6（#2749/#2754），合并103c9c456。后续上游变化是原生CI/事件顺序与login凭据位置规则下沉；GUI/HTTP契约与7d相同。103真实受控浏览器/Pi再次完成创建、两文件导入、显式启动与Chat导航；472项后端通过，唯一失败为本线agent_pins拆分漏登记严格resident清单，已补原composition来源和仍拒绝凭据读取的负例，产品逻辑未改。完整Python与外部厂商认证单独验收，不能由这些结果推定通过。

清单修复266c6463的后端累计481项和静态门通过；同名测试修复后b685全量为28392 PASS/20 FAIL，增量独立审核限定PASS。20项包含6个Darwin缓存环境失败、13个post-install快照差异及1个待归因teleport时序失败，不能报告全量通过。59647等旧手测服务已按用户要求停止；新dev合并后的服务和人工验收尚未完成。原始全量与审核证据见TODO。

以下数字与rebase记录保留为历史核查结果，以本节和当前候选验收为最新基线。

- 分支 `intg/gui-agent-journey`，PR [#2727](https://code.hyprial.com/HyprialOS/hyprial/pulls/2727)。
- Agent/平台方法盘点基于 `51f7efd1898251e5ac04ca331a9514c8cca2e51d`；后续登录/初始化修复更新到
  `f280850e01d7327ce89600255cf7af1a422342ae`，具体后端语义与契约变化见下方回归分类。
- 运行时注册表有 136 个声明、109 个 HTTP 方法，与该树 OpenAPI 一致；第三节的 16 个拟定请求均未注册。
- 对照共享页面、capability 装配、generated client、HTTP 参数、MethodSpec、daemon handler、legacy router 与 CLI/库实现。
- “存在”不等于“可用”：正式装配、权限、参数投影、真实外部响应分别验收；不能用 mock 或名称存在证明端到端通过。

### Rebase 与打包线核对（2026-11-13）

- 本地集成分支已 rebase 到 `dev@46a29ba48a23054408106816cc6e4d967e7e2391`，该版本已合入
  [#2742](https://code.hyprial.com/HyprialOS/hyprial/pulls/2742)。已核对 fetched PR head
  `40312fb07`、merge commit、源码、packaging-host-split 文档及 PR 正文。公网 API 起初 403，
  改用已配置的内部 Forgejo 客户端后成功读取；PR 明确不包含 #2727，也不包含本分支退出→登录循环。
- #2742 增加原生 setup、launch nonce/stop 与 external promotion 宿主协议，以及平台运行支持，
  不只是安装包样式。Web 的 `serve_gui` 仍负责同进程登录、初始化和退出循环；原生 external
  模式继续交给 `serve_external`，保留 launch nonce/stop。新增分流测试覆盖两个入口。
- rebase 前后 `gui/src`、`gui/server` 无差异；HTTP 方法仍为 109 个，未新增或减少，
  本文 16 个待补请求未因此实现。业务接线继续使用下述统一链，不改为 Tauri 调 CLI。
- 原生壳负责窗口、系统浏览器、启动/停止和打包验收；GUI 线负责共享组件及其宿主能力接点。
  Web 通过不能覆盖原生验收，宿主适配也不能另写账号状态或登录业务流程。
- macOS 打包登录不开浏览器：共享账号入口使用 `window.open`/`target=_blank`，正式装配未注入
  `openAuthorization`，原生窗口又拒绝新窗口。源码支持该阻断路径，实际安装包待打包线复测；
  不能据此认定 Casdoor 注册失败。跟踪 KP-056。
- 清理尚未开始。四步引导的共享前端和开发场景保留；旧 Web auth/setup/live 只在证明当前
  产品无消费者后删除。#2742 的原生 setup 和 external lifecycle 是活动入口，不能按旧快照批量删除。
- 已完成：GUI 焦点 378 PASS，全量 2102 PASS / 12 FAIL / 2 SKIP（12 项均 KP-050）；
  Python HTTP/onboarding/provider-key 焦点 743 PASS，launch contract 22 PASS；typecheck、build、
  generated client 一致性及产品范围 Ruff 通过。真实 Chrome + 隔离 daemon/HTTP + production Node
  两轮登录→初始化→退出 PASS（一个 GUI 页面、无额外 GUI 导航、无 JS 错误）；身份与设备提交为
  受控端口。全量 Python 结果另行更新。
- 真实 Casdoor 人工验收 PASS：用户在 rebase 后的隔离 Web `127.0.0.1:62734` 明确反馈
  “整段流程正常，包含新账号注册”，覆盖新账号注册、登录、设备初始化、退出本机登录、再次登录。
  测试 home/state 位于 `/tmp/hgui-rebase-cq6h4l_6`，独立 daemon 与从当前源码构建的 Tailcat，
  未使用 mock、日常 daemon 或日常配置。本记录不包含账号、凭据或授权链接；服务按用户要求保留。
  随后只读 `onboarding.status` 返回 authenticated/deviceReady/initialized=true、phase=serving、nextActions=[]。
- 原生 setup/shell 测试 38 PASS / 7 FAIL，相关文件与本次 dev 完全一致：三项仍匹配旧启动源码，
  四项测试未展开 `ACCOUNT_BRIDGE_HELPERS`；归打包线核对，不在 GUI 清理中改写断言。
  `ruff check .` 的 77 项错误均在本地未跟踪的历史探针，不改写或提交这些证据文件。

### 清理前的回归分类（2026-11-14）

- 清理前全量 Python：27132 PASS / 178 FAIL / 3 ERROR / 45 SKIP；不声明全量通过。
  该轮使用 Python 3.14，含继承的 agent/Git 环境及 macOS 长临时路径；失败中已观察到
  AF_UNIX 路径过长、继承 Git 变量导致断言差异。仍须逐项复跑，不能把所有失败都归为环境。
- 固定 `dev@46a29ba48` 的独立 worktree 对照：所选五文件 84 PASS / 7 FAIL。
  原生 request architecture、CLI inventory、凭据导入边界三组在 dev 同样失败；
  `test_cli_send_summary.py` 在清洁环境下通过。相关原生文件在本分支没有额外修改。
- 本分支新增的 method-registration 失败来自 `_bootstrap_methods()` 动态比较。
  改为显式冻结方法集合与 promotion 条件，保持 `onboarding.initialize` 仅在已安装
  promotion owner 时可用；不改 checker/baseline。修复后 registration、bootstrap、
  onboarding 与 HTTP 生命周期焦点 318 PASS。精确命令：
  `uv run --no-sync pytest tests/tooling/architecture/ipc/test_method_registration.py tests/daemon/onboarding tests/shell/request_http/bootstrap tests/integration/request_http/onboarding -q`。
- 独立审核 `wf-50fc3a1d11a649aa8ec24d8837d23200#review` 对固定
  `6da194a91cc8bc3cc5089824b14225b76aacb8fe` 给出 REWORK：方法注册修复 PASS，
  GUI 新增三处请求编排须修改；dev 与 HEAD 的五文件对照均 84 PASS / 7 FAIL。
- 本轮修复（不新增方法、不改 checker/baseline）：
  - `getAccountState` 只读一次 `onboarding.status`；pending selector 交登录控制器，
    独立 `readLoginOperation` 读一次 `auth.operation.get`，直接投影 authenticated/expired/denied/unknown，
    保留已有设备快照。刷新不再隐式发出 status→operation.get→status 三连请求。
  - 凭据 API 分为 `listKeys` → `user.provider.list` 与
    `listSecretGrants` → `agent.secret.list`；两个加载/错误状态独立，Key 选择器只读 Key。
    Auth 清单仍为独立 nativeAuth 读能力，不把服务密钥当原生 Auth。
  - GUI 初始化只写一次 `onboarding.initialize`。daemon 在同一可信 bootstrap context 内
    准备未就绪设备，再申请 promotion；设备 operation key 从 initialize key 稳定派生。
    pending/unknown 设备回执阻断重复准备，当前 transport 内未知初始化回执也不重发。
    GUI 不再直接 ensure、不在写 action 内轮询 status；宿主转场和独立状态观察负责恢复。
  - `INITIALIZE_SPEC` 增加既有 `DEVICE_ENSURE_FAILED` / `SUBMIT_OUTCOME_UNKNOWN`
    及 `DEVICE_ERROR_DATA`，已同步 OpenAPI/GeneratedClient；bootstrap-only 权限不扩展。
    serving 时仍不开放 initialize；当前 bootstrap 仍可见的 completed 回执幂等返回。
    既有独立 device.ensure 为兼容保留，nextActions 字段语义暂不变。
- PASS：GUI 架构门、request-contract、生成客户端、typecheck/build/production-copy；
  独立读取/初始化焦点 107 PASS，HTTP/onboarding/daemon 广焦点 530 PASS。
  红测试：独立凭据 3 FAIL→PASS、账号状态读 1 FAIL→PASS、单写初始化 10 FAIL→PASS、
  daemon 自动准备/未知结果/幂等 5 FAIL→PASS。
- PASS：真实 Chrome + 隔离 daemon/HTTP + production Node，在 1440/390 宽度完成两轮
  login→initialize→logout，同一个 GUI 窗口，无 JS 错误；HTTP initialize/logout 各 2 次、
  GUI device.ensure 0 次。设备准备由 daemon 完成；身份与设备端口受控，非新一轮外部 issuer 验收。
- 全量 Python 清洁环境对照（Python 3.12、短路径）：27265 PASS / 49 FAIL / 45 SKIP，
  仍非全套 PASS；失败继续在固定 dev 干净工作树逐项比对，不能统称环境失败。
  本轮代码完成后的全量、远端 CI 和新 SHA 独立复核另行记录。
- PR hygiene：正文 Submitted-by 首行修正后远端 run #45595 PASS（head `61c5e92233`），
  声明门通过；其余远端 job 尚未获得本轮通过结论。
- 原始日志仅存本地：`/tmp/gui-architecture-green.log`、`/tmp/gui-initialize-http-green.log`、
  `/tmp/gui-ci-full-clean-py312.log`、`/tmp/gui-architecture-e2e-17rh5wqn/result.json`。
  清理与第二阶段接线仍在本轮修复独立审查之后继续。
- `f280850` 已同步普通远端分支及 PR #2727；独立审核为
  `wf-40c31e39c6ad4dcb9a90ca8718f0fdb0#review`，要求从该完整 SHA 的独立工作树重新审查。
  GUI 全量为 2103 PASS / 12 FAIL / 2 SKIP，12 项均为 KP-050；干净工作树 Ruff PASS。
- 上一轮 49 个失败所在文件在固定 dev 的对照为 360 PASS / 38 FAIL / 5 SKIP：
  38 个失败 nodeid 均在原失败集合中。剩余 11 个尚待干净候选全量结果核对，不能统称环境失败。
  daemon request 架构门在 dev 与候选的完整输出相同（独立复核 611 findings / 14 violations）；
  此前“7 项”是局部归类，不是完整门禁计数。涉及源文件无本分支差异。
  门中的 base-growth 比较提交的例外清单，不代表重扫 dev 源码后的差异。
- 隔离契约总门：CLI 14/14、wire corpus 和场景登记校验通过；daemon-lifecycle runner 因
  `actor_host is required` 失败，在 dev 同样复现，涉及源码无本分支差异。
  HTTP request corpus 单独通过；总门后续步骤未执行，不记总门 PASS。原始日志位于
  `/tmp/gui-contract-f280850.log`、`/tmp/gui-contract-base-lifecycle.log`、`/tmp/gui-contract-f280850-http.log`。
- 远端 f280850 的声明门与 PR hygiene 通过，checks / cli-help-inventory 失败，Python 分片被跳过。
  当前 Forgejo 客户端只能取得执行状态，未取得失败 job 日志；不能将本地失败原因冒充远端根因。
  全量 Python 与独立审查仍在进行，未宣布 CI 或整包通过。已证实的基线问题统一跟踪 KP-057。
- 随后远端 f280850 的 mock-model-harness E2E 通过，GUI job 失败，具体远端步骤仍 UNKNOWN。
  本地复现 KP-054 的 760px 页面矩阵四处超时后，仅同步浏览器测试的登录/Agent fixture、菜单名称、
  归档新窗口行为及异步工具栏等待；四步引导产品代码与断言覆盖均保留。
  真实 Chrome 的 development/dist-parity 浏览器门修后 12/12 PASS（375/760/1440），
  原始结果在 `/tmp/gui-ci-parity-sync-green.log`。这证明共享展示等价，不是外部服务验收。
- 远端 `754e29c600b8ce731b84db4f36da9c5c3cd80e72`：GUI 的 push run #45623 与
  PR run #45625 均 PASS；hosted E2E、声明门、PR hygiene 通过。Python checks 与
  cli-help-inventory 仍 FAIL，pytest 分片未执行；不能将 GUI 通过扩展为整包 CI 通过。
- 固定 `f280850` 干净工作树的全量 Python 实际结束为 27212 PASS / 101 FAIL / 4 ERROR /
  49 SKIP（另有 7 XFAIL / 2 XPASS）。该轮测试环境准备不完整：外置 venv 不满足部分测试
  对本树 `.venv` 的要求，GUI 依赖未安装，且 PYTHONPATH 使用 `/tmp` 别名造成路径断言差异。
  这不是产品通过证据，也不能把 101 项全归为基线；补齐本树 Python 3.12 venv、GUI 依赖及
  `/private/tmp` 规范路径后，全部失败所在文件和未收集模块复跑为 **742 PASS / 35 FAIL / 6 SKIP**，
  无收集错误；35 个失败 nodeid 全部属于已在固定 dev 复现的集合，原 66 个额外失败已消除。
  这是环境修正后的定向复跑，不把两轮结果拼成一次全量 PASS，也不将 SKIP 当作通过。
  上轮 49 项中的 11 个未归属失败在这次全量均通过；当前 101 项中 35 个 nodeid 已在 dev 复现。
  原始日志 `/tmp/gui-ci-final-full-py312.log`，环境修正后复跑 `/tmp/gui-ci-prepared-failures.log`。
- 独立审核 `wf-40c31e39c6ad4dcb9a90ca8718f0fdb0#review` 已完成并接受结果：
  `f280850` 为 REWORK，仅登录回执并发读取阻断；凭据拆分、daemon 单写初始化、权限与幂等 PASS。
  审核者独立确认旧 49 项中 35 项在 HEAD/dev 同 nodeid 失败，14 项在清洁环境通过。
  Python 广焦点 530 PASS，上述 method-registration 精确命令新增测试后为 323 PASS；
  审核者未跑完整 27k/外部 issuer/原生，不扩大验收范围。
- 登录刷新/重新挂载竞态已复现：第一份 authenticated 回执清空 selector，第二个读者变为 unknown。
  修复为同 operationId 的在途读取合并；同一状态观察的终态保留给晚到消费者，新的状态读取或
  已准入账号操作使其失效；pending/读取失败不缓存、不自动重试，旧 operationId 的迟到回执仍拒绝。
  刷新/重挂载 2 个红测试、晚消费 1 个红测试均先失败；账号焦点修后 51 PASS。
  本轮只改 GUI 控制器与测试，不改 daemon、方法、契约或例外基线。
- 登录竞态修复后的本地回归：GUI 全量 **2112 PASS / 12 FAIL / 2 SKIP**，12 个失败均为
  KP-050 的 `playwright-cache.test.mjs`；无新增失败。账号焦点 51 PASS、Python onboarding
  广焦点 530 PASS；GUI 请求架构、结构/边界、glossary、生成客户端、共享入口门及构建 PASS。
  最终 production GUI 在真实 Chrome + 隔离 daemon/HTTP 上两轮 login→initialize→logout PASS，
  同一 GUI 页、无 JS 错误、initialize/logout 各 2 次、GUI ensure 0 次（1440/390）。
  身份/设备端口受控，不代表重验外部 issuer 或原生壳。
  日志 `/tmp/gui-login-race-*-final.log`，浏览器结果
  `/private/tmp/gui-architecture-e2e-zjqg5ueb/result.json`；原始日志不入库。
  `5a6e002717` 独立复审已 PASS（限定账号竞态与展示同步）：51 个账号测试、7 个独立探针、
  共享展示 12/12、Python 焦点 530 均通过。完整 Python、远端 job 日志、外部 issuer 与原生未独立复验。
  证据 `/private/tmp/evidence-gui-account-race-5a6e002-20261114/REPORT.md`。
- Harness CLI 已可查询结果及 inbox；MCP 通道仍可能报 STALE_SESSION，按现有 CLI 绑定继续，
  不重复派旧请求，也不将作者通道异常当作审核者掉线。原始审查证据位于
  `/private/tmp/evidence-gui-arch-f280850-20261114/REPORT.md`，不入本 PR。

### 旧 Web 通道清理（2026-11-14，T26）

- 删除无产品入口消费者的 `gui/server/auth`、`setup`、`live`，Web auth/setup factory、
  `platform/transport`、`HttpLiveClient`，以及 Python `shell/impl/gui/live` 和
  `run_gui_backend` facade。现有入口已明确拒绝旧 AUTH/SETUP 环境开关，拒绝规则保留。
- 删除只测上述实现的 worker/transport 测试及旧 `contract/e2e/gui_live` runner。
  旧 runner 要求已退役的进程桥，不把它的历史聊天 PASS 当作现在接线完成。
  `contract/gui-live/README.md` 改为退场说明，正式 MethodSpec/OpenAPI 未增减。
- 保留四步引导共享前端、所有归档场景、native auth/setup capability、AppChatAdapter、
  聊天/Lark 控制器与 #2742 原生 setup/external 生命周期。仅移除控制器的隐式旧 HTTP 回退；
  未注入能力时不连接，既有显式注入仍可用。未清理任何用户 home、journal 或已有数据。
- 保留原 Lark 组件浏览器交互断言，测试改成显式注入端口，覆盖授权、确认解绑、未知回执和重试；
  它只证明共享组件行为，不是后端/平台验收。四步引导由原有组件与双入口浏览器矩阵继续覆盖。
- GUI/daemon 架构及 Windows 例外仅删除已退场文件对应项；不改 checker、首次种子摘要或准入。
  GUI 架构测试原先硬要求旧业务服务仍存在且当前 baseline 等于首次种子，改为断言业务服务为零、
  当前 baseline 相对 HEAD 不增长；首次种子拒绝、增长拒绝与合成违规测试保持。
- 已测：新回退拒绝用例先 1 FAIL 后通过；GUI 焦点 48 PASS，全量 2086 PASS / 12 KP-050 FAIL /
  2 SKIP；共享展示浏览器 12/12 PASS，注入端口的 Lark 交互浏览器 2/2 PASS；CI 策略与 GUI 架构测试
  165 PASS。构建、typecheck、生产产物、客户端、parity、结构、边界、MethodSpec 注册/契约、术语及
  Windows/Ruff 门通过。GUI 架构发现由 35 减为 22，无违规；daemon 由 611 减为 601，
  以相同 base-ref 参数运行的 21 条违规与 dev 逐项相同（未带 base-ref 的原报告为 14 条）。
- Python 账号/HTTP/初始化/AppChatAdapter 焦点 918 PASS；受控身份/设备的真实隔离 daemon +
  production GUI + Chrome 两轮登录→初始化→退出 PASS（1440/390、单页、无 JS 错误）。
  外部 issuer 和原生没有重验。
- 独立审核 `wf-2032f27eb8514ce7b74fb5d791498fc0#review` 对固定 `ccca186d5` 返回代码/架构
  限定范围 PASS：GUI 焦点 52、全量 2086 PASS / 12 KP-050 FAIL / 2 SKIP，Python 焦点 1231 PASS；
  共享展示 12/12、注入 Lark 交互 2/2、受控真实 HTTP/daemon/Node/Chrome 两轮循环均独立复跑通过。
  证据 `/private/tmp/evidence-gui-cleanup-ccca186-20261114/REPORT.md`，不入库。
- 复审指出的过时 `dev-03-live-integration.md` 已删除，索引改指本对账；启动命令归现有
  `gui-dev-mode.md`。PR 已声明退场的 Contract-Impact；不靠增加方法或恢复旧通道过门。
- 完整 Python 已在隔离工作树、Python 3.12、清洁 HOME/环境完成：27222 PASS / 36 FAIL /
  45 SKIP / 7 XFAIL / 2 XPASS；无 ERROR。35 个失败 nodeid 与此前已在固定 dev 复现的集合完全一致。
  新增 `test_real_repository_first_seed_shape_is_reviewed` 仍要求当前缩减后的 baseline 哈希等于首次种子。
  修正测试为退役条目已移除、首次种子摘要不变，且当前清单不能用作新的首次种子；另加真实 Git fixture，
  证明删除实现并缩减清单可通过已有 base，而同一缩减清单不能首次播种。未改 checker、种子或 baseline。
  定向复跑 13 PASS / 1 既有 real-repository-baseline FAIL；新增修复已获独立增量复审 PASS（c2f4571ee）；全量未再次运行，不声明全绿。
  完整日志 `/tmp/gui-cleanup-python-full.log`，定向日志 `/tmp/gui-cleanup-seed-tests.log`，不入库。
- 远端 ccca186d5 的 GUI、声明、hygiene、模拟模型 E2E 已 PASS；Python checks/CLI inventory FAIL，
  pytest 分片未运行，具体远端日志未取得，不据本地结果推定原因。外部 issuer 和原生本轮仍 UNKNOWN。
  下述 16 个待补方法及 Agent 接线计划没有因此实现；
  下一阶段仍通过统一请求面补齐，不恢复旧 Web worker。

## 唯一正式调用链

共享组件 → capability/API adapter → GeneratedClient → HTTP `POST /daemon/request/<method>`
→ HTTP 层按 MethodSpec 校验参数、可信身份和准入 → `request_once` 进入 daemon request
→ 注册表绑定的 handler → 已有 daemon/identity 业务权威。

MethodSpec 是声明和准入依据，不是另一套业务实现。CLI 可能直接调用公开库，也可能走 legacy daemon RPC；
这两种情况都必须核查，不能仅因 OpenAPI 没有名字，就断言后端业务不存在。
前端不能调用 CLI、读写本地配置库，或继承宿主环境变量绕开此链。

源入口：[GUI 装配](../../gui/src/app/capabilities.tsx)、[HTTP 分发](../../src/hyprial/shell/impl/request_http/server.py)、
[注册表](../../src/hyprial/daemon/impl/request_catalog/registry.py)、[legacy router](../../src/hyprial/daemon/impl/ipc/router.py)。

## 一、现有组件与读写逐项对应

### 凭据设置

组件：[CredentialsSection](../../gui/src/features/connections/CredentialsSection.tsx)，
[正式 adapter](../../gui/src/features/connections/shared-credentials-api.ts)。

| 页面信息 / 操作 | 当前方法与真实含义 | 当前缺口 / 计划 |
|---|---|---|
| 列表、刷新、Key 名称/服务商/授权 Agent、详情 | `user.provider.list`；另读 `agent.secret.list` 显示服务秘钥授权记录 | 两者均已注册/HTTP/GUI 接线；后者不是原生 Auth 清单 |
| 添加内置 Key | `user.provider.login`：provider/kind/label/secret/environmentNames | 方法已存在；正式 owner store/身份映射/信任与签名装配受 KP-030 阻断，不新增另一套保存接口 |
| 更换 Key | `user.provider.rotate`：keyId/replaceSecret | 已接；已有进程需重启采用新值 |
| 删除 Key | `user.provider.logout`：keyId | 已接；不要与账号 `auth.logout` 或 Agent 原生文件退出混淆 |
| 撤销 Agent 的 Key 授权 | `agent.provider.revoke` | 已接；不等于立即清除已运行进程中的凭据 |
| 自定义协议、服务地址 | 当前正式 `user.provider.login/list` 无对应字段，正式添加拒绝 endpoint | 扩展这两个既有方法和 Key 元数据、授权后的启动物化；先限定 Pi，不新增 custom CRUD 分类 |
| 添加/更换时“测试” | 只有前端可选 `testKey` 端口；正式组合未提供 | 已批准待实现 `user.provider.test`，细节见第三节；mock 通过不代表真实测试 |
| Auth 只读记录 | 共享 Auth 清单 UI、隔离 mock 与正式元数据读取已实现，97e7966 独立审核限定范围 PASS | 统一清单投影 `agent.provider.auth.list`，支持按 Agent 筛选，每条附状态；创建/导入即出现记录，不等启动才登记，不另增 status |

共享 Key 库/handler 已有完整代码路径：[provider_keys](../../src/hyprial/daemon/impl/provider_keys/ipc.py)、
[装配 context](../../src/hyprial/daemon/impl/provider_keys/context.py)。
此前隔离 HTTP 已返回 `PROVIDER_STORE_UNAVAILABLE`，见 KP-030 和下方启动组合；
本机通过 daemon 环境继承、旧 secret set 或原生文件启动，不能证明这条共享 Key 通路完成。

2026-11-14 在 `ccca186d5` 继续核对 T10：缺口还包括真实绑定签发与发布，不只是 resolver 装配。
`user_key/ipc.compose` 创建默认 disabled 的 `Signer(store)`；`user_key/publication.publish`
拒绝 `USER_KEY_UNBOUND`；`shell/impl/login/user_key.initialize` 只创建本地 unbound key，未调用 issuer。
`composition/cells/spaces.CellSpaces.private_space` 已有 owner-home，但未提供 provider 记录的身份映射与写入准入。
这与 [#2195 已合入说明](https://code.hyprial.com/HyprialOS/hyprial/pulls/2195) 及
`contract/identity/user-key.md` 的“local verification, not production issuance”一致。
后续先确认正式发行/撤销/发布能力及归属，再接 owner store、验证器、签名和受保护记录准入；
不把本地自签、测试 issuer 或打开 `owner-keys` 当作完成。此依赖不阻塞独立的原生 Auth 清单接线。
同日 cc-daemon-request-review 只读复核确认同一缺口（消息 `f1761c43-9ddf-58ce-af91-6ebe3f9893ce`）：
U2 验证来源提交 `63db6c3d5916afb21fbda75c83923c238a33db8b`，发布保留拒绝来源
`2b6d8e6cd047a2628c1af6c2e6fa2afa39359558`；未找到可直接接入的生产实现。
已有飞书 unionId/org binding proof 不是 owner-key binding，不可替代签名信任来源。

### 平台接入

组件：[列表](../../gui/src/features/connections/PlatformIntegrationsSection.tsx)、
[详情](../../gui/src/features/connections/PlatformIntegrationDetail.tsx)、
[正式 adapter](../../gui/src/features/connections/platform-integrations-api.ts)。

| 页面信息 / 操作 | 现有能力 | 当前缺口 / 计划 |
|---|---|---|
| 列表、刷新、运行状态、绑定 Agent | `adapter.list`；单项 `adapter.status`，pin 信息也有 `adapter.pins` | 已有 HTTP；列表返回 configured/desired/processRunning/online/error/agent，不用一个 online 代替全部 |
| 启动 / 停止 | `adapter.start/stop`，参数 name | 已有 HTTP 和 GUI 接线 |
| 重新加载配置 | `adapter.reload`，参数 `{}` | 已有，当前是全局 reload；不能把按钮包装成“仅重载此接入” |
| 使用已有应用，提交 App ID/secret | CLI `adapter add` → `_adapter_add_existing` → `add_lark_gateway` → reload | `adapter.register` 已接 daemon handler/MethodSpec/HTTP/GUI，待独立审核；保存/加载与授权/启动分别判定 |
| 创建新应用、打开授权页、读取进度、取消 | CLI `adapter add --new` → `_adapter_onboard` → `resolve_lark_app_credential` → SDK `register_app` → `add_lark_gateway` | 已接 `adapter.onboard.begin/operation/cancel`，待独立审核；复用注册实现，daemon 持有可查询操作生命周期 |
| 权限状态、已授权/缺失项 | `adapter scopes check` → `diagnose_scopes`；`scope_inventory` 已有投影 | `adapter.authorization.status` 已补 daemon/HTTP/GUI，独立审核 PASS；沿用能力 ID，不本地维护中文权限翻译；如展示原始 scope，消费诊断里的 matchedScope/anyOf |
| 发起授权、刷新操作、取消 | `LarkAuthorizationService` 已有 `start/operation/cancel/status`，CLI 已有授权流程 | 已实现 `adapter.authorization.begin/operation/cancel`，独立审核 PASS；begin 复用 service.start，不重写平台授权逻辑 |
| 同步平台身份 | CLI → `configured_identity_gateway` / `sync_identities`，写本地身份 store | 已实现 `adapter.identities.sync`（ad135057d 独立复核限定通过）；是写操作，部分错误不可显示完整成功 |
| 读取同步结果列表、类型、采集状态、时间 | CLI `adapter identities list` → `open_identity_store` | 已实现 `adapter.identities.list`（ad135057d 独立复核限定通过）；沿用 kind/user-bot-app、standing、source、first/lastSeen、unionId/hyprialOwner；无绑定不能编造 owner |
| 查询平台用户对应的 Hyprial 用户 | **`identity.resolve` 已有 HTTP**，unionId 或 adapter+openId | 已接平台身份表的用户行“查询绑定”，一次显式点击一次读取，无自动逐行查询；结果显示后端 user/kind，verified=false 单独显示仅出站绑定。未绑定/冲突/读取失败分别展示；不推断邮箱或 App 创建者，不新增方法 |
| 删除确认预览 / 删除 | CLI 先确认已停止 → **`management.adapter.remove`** → `remove_lark_gateway` → reload | 已有 legacy daemon 删除；补 `management.adapter.remove.preview`，原名 execute 只收 operationId，绑定目标版本和可信调用者；领域内预约、停止、删除事务、settlement 防止并发启动，不使用前端拼接预览代替协议 |
| 企业微信、WhatsApp | 当前未接此流程 | 保持不可选，不扩成一套空后端 |

关键实现：[CLI admin](../../src/hyprial/shell/impl/cli/commands/adapter/admin.py)、
[CLI identities](../../src/hyprial/shell/impl/cli/commands/adapter/data.py)、
[平台注册/删除/路由库](../../src/hyprial/daemon/impl/bootstrap/adapter_registration.py)、
[授权服务](../../src/hyprial/daemon/impl/adapters/lark/credentials/authorization/service.py)。

旧台账中的 `adapter.remove`、`adapter.remove.preview` 不作为已确定的新方法继续排单：
先复用 `management.adapter.remove`。如果以后统一重命名，必须是同一 handler 的迁移及旧消费者退场，不能双实现。
`adapter.onboard.preflight` 和 `adapter.identities.find` 也暂不加入最小补齐包：
本轮 UI 没有独立消费需求，前者的校验可归 begin，后者不代替已经存在的 `identity.resolve`。

### Agent 列表、创建与配置

组件：[AgentForm](../../gui/src/features/agents/impl/AgentForm.tsx)、[AgentList](../../gui/src/features/agents/impl/AgentList.tsx)、
[AgentDetail](../../gui/src/features/agents/impl/AgentDetail.tsx)、[API](../../gui/src/features/agents/impl/api/index.ts)。

| 页面信息 / 操作 | 当前调用 / 事实 | 当前缺口 / 计划 |
|---|---|---|
| 目录、刷新、所有者/主机、harness/模型/cwd | `agent.list` | 已有 HTTP；邮箱/主机按返回值，不把缺失 owner 猜成“我” |
| 搜索、运行中/未运行/未知筛选 | 在 `agent.list` 结果上筛选 | 不新增过滤或搜索 RPC |
| 名称、harness、可选工作目录创建 | `agent.create`，preferredHarness/cwd | 已有；默认 workspace 自动建立，自定义 cwd 提交绝对路径；**当前创建不自动启动** |
| 创建时选 Key | 创建成功后 `agent.provider.allow` | 已有；Key 正式通路另受 KP-030 阻断；部分失败保留已创建 Agent，只补凭据，不重复 create |
| 创建时选 Auth | 创建成功后 `agent.provider.login` | 已有；Claude `.credentials.json`，Codex `auth.json`，Pi 自己的 `auth.json` + `models-store.json`。文件名/数量检查不证明 JSON 内容或厂商认证有效 |
| 改显示名 | `agent.rename` | 已有；不改稳定身份/actor |
| 概览运行、活动信息 | `agent.list.runState/activityHints` | 读已发布字段；缺少观测不能伪造 idle/正常；运行中不等于完成一次模型响应 |
| 概览需要注意 | `agent.provider.status` + `link.list` | 有限的已读异常，不是全局健康诊断 |
| 启动/停止单按钮 | `agent.start` / `agent.stop`，之后 `agent.list` | 已有；accepted 不代表 running；未知结果先读，不重复提交 |
| 模型设置/更换 | 停止后 `agent.start{provider,model}` | 已有；会写回记录，失败也可能已保存。无需 `agent.model.set`，无需为手填模型新增 models.list |
| 更换 Key / 重新导入 Auth | `agent.provider.allow{replace:true}` / `.login{force:true}` | 已有；只更新所选类型，不删另一类；现有链没有通用“活动凭据切换”语义，不能包装成一键切换并验证成功 |
| 移除原生 Auth | **`agent.provider.logout` 已有 HTTP**，前端已有 AgentProviderApi.logout / AgentsApi.logoutNative | Agent 详情已加入移除与确认，复用 logout；正式元数据读端口已接并独立审核通过，全局凭据设置保持只读。旧 AgentProviderSection 的“退出服务”未被当前页面使用 |
| Auth 导入记录与启动结果 | login 回执已有文件 path/bytes/replaced/unchanged；元数据读回已接，历史导入/启动证据仍缺 | 用下述只读投影；展示“已保存”，有版本关联启动证据才显示时间。同一 Agent 可能同时有 Key，不能仅凭 running 宣称此 Auth 已验证 |
| 平台绑定 / 解除 | `adapter.list/pin/unpin`，写带 expectedActor | 已有 HTTP；绑定不要求 adapter 在线；不允许静默抢占别的 Agent |
| 消息路由 | 见下表 | 业务已有，RPC/HTTP 和 GUI 控件仍待补 |
| 所属组织 | 用户确认暂不开放 | 保持禁用；不为本轮扩充 grant/org 接口 |
| 删除 | **`agent.destroy.preview` / `agent.destroy` 已有 legacy daemon** | 补原名 MethodSpec/HTTP/正式 UI；当前 mock 已支持。现有 preview 只给 workspace 摘要，尚非 UI 所需完整副作用/并发确认凭据 |
| 工作目录选择 | 当前路径输入；创建用 `agent.create.cwd` | 可信宿主目录选择器待接；不是新增 daemon folder-picker 方法 |
| 创建后修改工作目录 | CLI start --cwd 有启动通路；正式 `agent.start.cwd` 已补可选参数 | 停止后在启动弹窗预填当前 cwd，保存并启动；有效绝对路径覆盖旧启动 intent，复用 harness 目录检查。目录无效或 Agent 正在运行时不更新记录；进入启动后失败不回滚已保存设置。省略/null 保留既有目录解析，不新增 cwd.set |
| Pi 启动的工作目录信任确认 | 既有 `agent.start` 增加可选布尔 `approve`，同步 MethodSpec/OpenAPI/GeneratedClient/共享启动弹窗 | `true` 仅 Pi 且必须显式绝对 cwd，严格解析后保存规范路径；这是目录路径级信任，不是目录 inode/仓库内容身份锁；批准与 cwd 随 desired launch intent 保存，自动恢复继续使用，不是一启动即失效。`false` 前置拒绝且不被 stored --approve 覆盖；省略兼容原调用，false 不撤销旧批准；停止移除运行 desired intent，关闭自动恢复。页面每次打开/修改目录清除确认，未确认零请求；不写 create.harnessArgs，不新增 RPC，不绕过 Pi guard。真实 Chrome→Node→HTTP→daemon→Pi 启动、导航 Chat 已在隔离环境通过；模型及身份服务受控，厂商认证 UNKNOWN |
| 创建后改 harness | 当前 UI 固定创建选择，单一展示 | 从本轮 TODO 移出；不为旧的“默认/运行 harness”双入口补 setter |

原生导入实现：[provider_credential.py](../../src/hyprial/daemon/impl/application/actors/agents_admin/provider_credential.py)。
删除实现入口：[registry_ops.py](../../src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py)。
`agent.config.rebind` 确实存在，但参数是 source/rollbackReceiptId/dryRun，处理配置树发布与回滚，
**不是工作目录编辑**；`agent.migrate.*` 是 home 迁移，也不能直接拿来充当编辑 cwd。

### harness、凭据与启动组合

| 创建时 harness | 后端声明的 provider 范围 | 原生 Auth 导入 | 启动条件 / 真实链路限制 |
|---|---|---|---|
| Claude | anthropic / deepseek / hyprial | Claude `.credentials.json` | DeepSeek 转 Anthropic 接口；Hyprial 必须有 `vendor/model`；原生文件与显式 Key 同时出现会拒绝，不能当成自动切换 |
| Codex | openai / deepseek / hyprial | Codex `auth.json` | DeepSeek 仅 `deepseek-flash`，使用 Responses；Hyprial 必须有 `vendor/model`；默认 file 凭据模式要求 agent-owned auth.json，仅注入 OPENAI_API_KEY 不等价于完成原生认证 |
| Pi | Pi 已注册服务商（包含 kimi-coding / moonshotai 等）；hyprial 明确拒绝 | Pi 自己的 `auth.json` + `models-store.json` | 显式 provider/model 成对；选择校验器接受任意字符串，不代表 SDK 有目录项；GUI 自定义 endpoint 正式写入仍不可用 |

这些是后端选择/文件规则，不代表 GUI 共享 Key 已跑通。Claude/Codex/Pi 仅导入自己的原生格式；
Pi 的两个文件是一条 Auth 记录，不能将 Claude/Codex JSON 改文件名冒充 Pi。导入只检查文件集合、非空及安全落盘，
厂商认证和模型响应要在实际运行中验证。`agent.provider.login.provider` 是 harness ID，`agent.start.provider` 是模型服务商 ID。

当前 GUI 创建只写记录和凭据，然后进入配置：Hyprial Key 的 `vendor/model` 在启动前填写；
Codex DeepSeek 只允许 `deepseek-flash`；Pi 显式 provider/model 成对。模型可停止后更换，启动失败也可能已经保存记录。
Codex 共享 OpenAI Key 不会自动生成默认 file 模式所需的 auth.json，所以该选项不可用；自定义 endpoint 待链路补齐后仅面向 Pi。

h2b-monitor/emergent 的运行实例证明宿主环境注入、原生登录或旧 secret 通路能运行部分组合，
并不证明 `user.provider.login → agent.provider.allow → agent.start` 已闭合。Pi SDK 本机探针与分发版本不同，
Kimi/Moonshot 的真实模型回复仍须分别验收，不能按服务商字符串被接受便宣布支持。

### 路由、聊天、首页与快捷对话

| 页面信息 / 操作 | 已有实现 / 方法 | 补齐范围 |
|---|---|---|
| 飞书群 @机器人后读取候选 | `adapter.routes.candidates` → `observed_chats(strict=True)` | 已接共享页面与 HTTP；只读当前接入 namespace 的入站观测，缺库为空、读取失败报错；受控浏览器通过，真实飞书待验收 |
| 读取路由、保存、删除 | `adapter.routes.list/add/remove` → `list_gateway_routes/add_gateway_route/remove_gateway_route`；写后 reload | 独立限定 PASS（0f4f55de2）；写入带完整接入配置 revision，锁内校验；默认路由删除需明确 clearDefault。保存与运行加载分开，未知回包只读核对，不自动重发 |
| “开始对话”与聊天列表 | 当前前端打开指定 Agent；历史由 `conversations.list` / `message.query` 发现 | 不自动把新建 Agent 加进聊天，不新增 chat.create；无消息的前端打开记录目前不跨刷新持久化 |
| 目标目录 | `targets` 已支持 kind=agent/user/channel_route | 当前 GUI 只请求、接受 agent；要展示平台目标需改现有 adapter |
| Agent 历史 | `message.history` 已接；`conversations.list/history` 也已有 HTTP | 当前 GUI 用 conversation 列表的 legacyConversationId 读 message.history；canonical history 可接现有 conversations.history，不另造 history 方法 |
| 发送给 Agent、未知结果查询 | `message.send` / `message.send.key_lookup`；另有旧形态 `message.send.lookup` | 已有正式链；必须保留 operation key/幂等核对，不能因超时重复发送 |
| GUI 主动发送到 route | daemon message.send 有 route 分支和 routeDelivery 回执 | **HTTP 目前只准单个 Agent URI**，schema 探针确实拒绝 route。须扩展现有 message.send 的 HTTP 目标投影、权限、结果查询和 GUI 校验，不新增 route.send |
| 飞书入站与回到飞书 | 已有 adapter 入站/出站路由 | 配路由不等于自动镜像所有 GUI 消息；跨端身份、参与者可见历史和回复归路仍需真实验收 |
| 首页 Agent 数量/正常异常 | 后端 `agent.list.runState.state/certainty/observationHealth`，前端 `runStateFactsOf` 派生 summaryState | 只有 observed + current 才计数；idle/in-turn 为正常，其余已映射且非未知的状态为异常；无法派生时保持未知。summaryState 是前端字段，不是后端字段 |
| 首页平台绑定数 | `adapter.list` 的绑定、online/processRunning/error | 有正式读取；未绑定接入不算绑定；mock 随同一状态变化 |
| 首页任务数 | 当前正式 StartPage 没有 task summary adapter | `workflow.list`、`routine.list/status` 都已有 HTTP；先定义任务计数口径再投影，不能据此新增 task.summary |
| Squire / 个人侍从 | 普通 Agent 创建/凭据/启动方法已有；旧 `management.squire.ensure` legacy 在场 | 当前快捷选择有前端 identity store；旧 management.squire.prepare 虽有 spec 文件但**未绑定进注册表**，且固定 Pi/DeepSeek。不能宣称已开放，也不新建 Squire 业务；引导复用普通 Agent 链，用户选择需单独接 UI |

路由源：[CLI routes](../../src/hyprial/shell/impl/cli/commands/adapter/routes.py)。
GUI 操作：创建 Agent 后进入平台绑定 → 绑定一个接入 → 平台收到消息后点「读取候选」→
填写路由名称、选择目标聊天，可勾选默认 → 保存路由。读取与写入独立请求；pin 为入站绑定，
route 为命名外发目标，配置成功不代表平台在线或已经完成一次对话。共享 Key 的 T10 仍属本轮计划，
不以 BLOCKED 条目代替其正式依赖核实与接线；未经验证不宣布包含 Key 的整段流程完成。
聊天源：[connected-chat](../../gui/src/features/chat-channels/impl/connected-chat.ts)、
[HTTP send 约束](../../src/hyprial/daemon/impl/request_catalog/message/send/specs.py)、
[参与者历史读取](../../src/hyprial/daemon/impl/application/messaging/inbox_surface/history/__init__.py)。
GUI 只能读当前可信主体有权查看的历史；“像 TUI 一样完整”不能解释为无授权读取 Agent 全部 inbox。

### 前置登录、设备与状态读数

当前点击路径：启动器打开 GUI → 用户设置 → 登录 Hyprial → 外部授权页注册或登录 →
返回 GUI → 设备 → 初始化当前设备（一次 initialize，daemon 内准备设备并初始化）。只有 authenticated、deviceReady、initialized、serving 均满足才读取设备详情。
初始化和退出在原 GUI 窗口恢复；授权网站独立打开。未登录业务页显示登录入口，不读旧账号数据。

| 操作/信息 | 现有 MethodSpec / HTTP 方法 | 行为与边界 |
|---|---|---|
| 账号/初始化状态 | `onboarding.status` | authenticated/deviceReady/initialized/phase/operations；不从单个 accepted 回执推断就绪 |
| 登录、进度、取消 | `auth.login.begin` / `auth.relogin` / `auth.operation.get/cancel` | 本地 GUI 不直接创建账号；用户在真实授权网站注册，再完成本机授权 |
| 准备设备、完成初始化 | `onboarding.initialize`（daemon 内复用 device.ensure 能力） | 同账号同机器可复用设备材料；新账号/机器依后端校验。新 bootstrap 生命周期仍须按状态初始化，不复用旧 generation 回执 |
| 退出本机 | `auth.logout` | 清理登录材料并轮换会话，可能停止 daemon 及打断运行；保留 Agent/设备材料/历史，不退出 Casdoor 网站。不把部分删除当完整成功 |
| 本机状态 | `daemon.status` | 显示实际服务阶段与源码版本；版本号不是已发布证明，不推断网络在线 |
| 服务更新状态 | `autoupdate.status` | 自动升级开关、调度及最近记录；每日计划不等于开启。GUI 设置入口待后续 KP-049 |
| 升级迁移记录 | `upgrade.steps.status` | 迁移状态、次数、时间、旧文件/进程计数；旧进程数是系统进程扫描，非日志或当前 home 专属；扫描失败返回零不能证明无旧进程 |
| 运行诊断 | `top.snapshot` | 观测时间、Agent/滞留收件人条目数，不等于健康数 |
| 日志事件摘要 | `log.page` | 安全事件投影、每页 20 条、cursor/上下页；不含消息正文，不承诺全局时间排序 |
| 主机列表 | `hosts` | 已有 HTTP，当前设备页不消费；是 Hyprial presence，不是纯 Tailscale 列表，也非所有读取都以加入组织为前提 |

正式适配入口：`features/account/impl/connected-account-actions.ts`、
`features/settings/impl/device/device-actions.ts`、`read-adapters/`、`diagnostic/`、`logs/`；
后端：`application/onboarding/{gui_host,service,runtime}.py`。

已验证范围：人工反馈真实账号登录/设备初始化/退出再登录正常；独立审查覆盖受控身份的真实 HTTP/daemon、
同窗口两轮循环、旧 bearer 失效、部分退出回执与 30 秒恢复期限。它们不等于原生壳或全部故障恢复验收。
持续连接失败超时后停止轮询、允许重新检查；结果未知不重放业务写。
初始化失败后跨进程恢复仍有 KP-059，长 socket 路径与浏览器强刷验证仍见 TODO T05–T09；本轮未重测真实 issuer。

## 二、复用现有名字，优先补声明/装配/投影

| 任务 | 精确入口 | 应做的工作 |
|---|---|---|
| 共享 Key 正式可用 | `user.provider.login/list/rotate/logout`、`agent.provider.allow/revoke/status` | 修复 KP-030 所列 owner store/信任/签名/准入装配及启动注入验收；没有新增 RPC 的理由 |
| 自定义 Pi Key | 扩展 `user.provider.login/list`，连通既有 allow/start | 加 endpoint/protocol 的持久字段和安全投影，进入 Pi 运行配置；旋转保留配置；不保存测试模型 |
| 删除 Agent | `agent.destroy.preview`、`agent.destroy` | 原 handler 注册 MethodSpec/HTTP；完善副作用预览、身份和状态期望、未知结果读取 |
| 删除平台 | `management.adapter.remove.preview`（待新增）、`management.adapter.remove`（已有） | 配对预览/确认；停止、版本认领、并发启动排除和运行快照更新归 daemon 权威 |
| 平台身份归属查询 | `identity.resolve` | 只补平台页调用、未绑定/仅出站/verified 语义投影 |
| 移除 Auth | `agent.provider.logout` | 详情交互已实现；补可信清单读取及正式读回，不另加删除方法 |
| 平台主动发送 | 扩展 `message.send` 及既有查询 | 不放宽成任意收件人；定义 route 授权、幂等目标归一化、查询与错误投影 |
| 创建后 cwd | `agent.start.cwd` 已扩展 | 复用 harness 目录校验和既有 create-before-start 写入顺序；不搬动 Agent home，不新增目录选择 RPC；真实模型启动另验 |

## 三、请求入口补齐计划与实施状态

下表逐项注明实施状态；Auth 清单、平台身份 list/sync、adapter.register、adapter.authorization.* 已独立PASS；onboard三个操作已独立PASS，routes四个方法已接线自测；剩余新请求为 user.provider.test。既有方法装配与真实启动验收另计。
参数/回执字段以当前 MethodSpec/OpenAPI 为准，不能把未实施项当作已有 generated client。

| 拟定方法名 | 输入 / 输出边界 | 复用来源与新增的实际内容 |
|---|---|---|
| `user.provider.test` | provider、secret、model，可选 endpoint/protocol → 有界模型响应判定/脱敏错误 | 现有 secret set 验证只查字段形状；`agent tiers probe` 调用 `probe_profile`，依赖已配置 profile/harness，默认写运行能力，不是未保存 Key 的一次性测试。可复用底层协议/探测能力，不能直接冒充同一契约；不创建 Agent/会话、不保存 Key/模型、不自动重试 |
| `agent.provider.auth.list` | 可选 actor/harness 筛选 → 当前可信用户有权读取的原生 Auth 记录，每条含 Agent/harness、文件存在情况、导入时间/启动证据为空时保持 null | 元数据读取已实现并独立审核通过；复用 native credential 存储。导入版本及启动关联仍未实现，不从运行状态推断；全局页读清单，详情页传 actor，同一查询不做逐 Agent N+1，也不另增 auth.status。只读元数据，不返回文件内容、token，不推断有效期或厂商成功；某条读取失败不得伪装成没有 Auth |
| `adapter.register` | name/provider/appId/appSecret → configured/reloaded/nextAction、无密钥的 adapter 元数据 | 已实现且独立 PASS；复用 add_lark_gateway，保存后 daemon 加载配置，失败保留已保存回执；不覆盖同名、不自动启动 |
| `adapter.onboard.begin` | name/provider/operationKey → operationId、授权地址/进度 | 已实现且独立PASS；复用平台注册 SDK；daemon 持有有界 worker 与私有回执，不在 HTTP 请求线程等人操作 |
| `adapter.onboard.operation` | operationId 或 name（互斥）→ 当前阶段、保存/加载结果 | 已实现且独立PASS；只读同一回执，页面重开按 name 恢复；不得返回 secret |
| `adapter.onboard.cancel` | operationId → 取消结果 | 已实现且独立PASS；回收本地 worker；已建远端 App 不因本地取消而被虚称已回滚 |
| `adapter.authorization.status` | name → state、capabilities/missingCapabilities、appId、checkedAt（毫秒）、可选脱敏 reason | 已接线且独立 PASS；复用 scope_inventory/diagnose_scopes；查询失败保持 unconfirmed，列表不做逐应用网络读取 |
| `adapter.authorization.begin` | name、明确批准的能力、operationKey → operationId/URL | 已实现且独立PASS；复用 LarkAuthorizationService；必须显式包含三项必需能力，选填能力不会自动申请 |
| `adapter.authorization.operation` | operationId 或 name（互斥）→ 状态/授权结果 | 已实现且独立PASS；按 name 恢复最近一次操作；无记录为 ADAPTER_OPERATION_NOT_FOUND |
| `adapter.authorization.cancel` | operationId → 取消结果 | 已实现且独立PASS；停止等待并回收所持进程，不撤销平台已授予权限 |
| `adapter.identities.sync` | name → bot/chats/chatsComplete/identitiesRecorded/errors | 已独立审核通过；复用 sync_identities；写入观测，保留已有绑定；不能新建 Hyprial 账号 |
| `adapter.identities.list` | name → 类型/平台标识/展示名/unionId/已知归属/standing/source/时间 | 已独立审核通过；复用身份 store 的既有查询与字段投影 |
| `adapter.routes.candidates` | name → candidates[{chatId,chatType}]，类型未知为 null | 独立限定 PASS；复用 observed_chats，只读当前接入的观测，不猜 chat ID、不吞数据库失败 |
| `adapter.routes.list` | name → adapter{name,provider,appId,routes,defaultRoute,revision} | 独立限定 PASS；复用 list_gateway_routes，隐藏文件路径与 credentialRef；revision 为完整接入配置的摘要 |
| `adapter.routes.add` | name、route、chatId、makeDefault、expectedRevision → adapter/configured/reloaded/nextAction | 独立限定 PASS；复用 add_gateway_route；锁内比较 revision，同名不覆盖；一次写入后尝试加载，不启动平台 |
| `adapter.routes.remove` | name、route、clearDefault、expectedRevision → 同上 | 独立限定 PASS；复用 remove_gateway_route，默认项需显式确认；未知结果读回，不自动重发 |

这些方法统一走第一节的链。平台和路由主要是**把已有 CLI/公开库能力收进 daemon 请求面**，
不是重新实现业务；异步 begin/operation/cancel 的生命周期、准入和失败恢复确实需要补齐。
`agent.readiness`、`agent.runtime-context`、`ProviderAuthAuthority.status()` 都不是原生 Auth 清单：
前两者涉及运行准备或环境探测，后者是认证协调器聚合状态；不能借“已有 status”暴露不相干数据。

## 四、清理与明确不做

- 迁移通用平台请求后，删除旧 GUI-only `lark.adapters`、`lark.authorization.status/start/operation/cancel`、
  `lark.binding.set/clear` 路径。顺序是先迁移并验收，再删除旧消费者；无产品消费者的 `withLiveAuthorization` 已移除。
- `chat-channels/.../lark-binding.ts` 虽名字带 lark，实际调用 adapter.list/pins/unpin 和 targets；不能按文件名误删。
- 不删除仍有 CLI/agent 消费的 `agent.secret.provider-write`、原生 login/logout、授权库或路由库。
- 不新增 `agent.model.set`、`chat.create`、`route.send`、独立筛选方法、另一个 Key CRUD 或目录选择 daemon RPC。
- 组织功能、修改创建时 harness、home 迁移、能力账本、自动升级设置均不扩入本轮。
- 工作目录选择器、任务计数口径、Squire 快捷入口属于宿主/前端组合问题，不能全部打包成“后端方法缺失”。

## 五、验收顺序与当前结论

1. 先完成已有方法的装配/投影，明确 Key 与 Auth 两条路径；原生 Auth 导入后可读回，重导入清除旧验证关联。Auth 读回不得因 Key store 不可用而一起丢失；两组读取独立呈现成功/失败。
2. 添加/授权/启动平台接入，读实际权限与身份；已绑定信息用 identity.resolve，未绑定不伪造成功。
3. 创建 Agent → 写所选凭据 → 配置模型 → 显式启动 → 读取运行结果 → 点击开始对话 → 真实回复。
4. 平台绑定 → 飞书群 @机器人 → 读取候选 → 保存/读取路由 → 平台回复及 GUI 可见历史核对。
5. 最后验证停止、换凭据/模型、重启、删除以及未知回执恢复。每项分别记录 mock、契约、真实 HTTP、浏览器、外部服务结果。

- **PASS（声明/源码核查）**：注册表与 OpenAPI 一致；上表动作已定位已有调用链、缺失层或明确不做范围。
- **PASS（隔离焦点）**：113 tests，覆盖 adapter 请求目录、Agent 生命周期/原生凭据声明、CLI route/remove 的既有行为。
- **FAIL（当前能力对目标旅程的差距）**：原生 Auth 的导入/启动版本证据未完成、平台设置与路由缺正式请求入口、HTTP route send 被拒；不能说每个控件都已正式接线。
- **BLOCKED（已有证据）**：共享 Key 正式存储装配 KP-030；失败初始化跨进程恢复 KP-059。
- **UNKNOWN / NOT RUN（本轮）**：真实厂商认证/回复、真实飞书全链、全部页面重新点击、真实 issuer、原生壳；焦点测试不替代这些验收。

## 六、Auth 清单与 CLI / Lark 收敛补充

本节定义迁移边界；后端入口仍按第三节规划，不在清理文档时自动实现。

### 一个 Auth 清单接口，状态放在条目中

- 将原 `agent.provider.auth.status` 提案改为 `agent.provider.auth.list`，不并存两个请求。
- Key 正式方法全名是 `user.provider.list`，不是 `user.list`。本页已退役的是旧
  `agent.secret.provider-write/grant` 的 Key 写入入口，不是删除所有 secret 业务：
  冻结契约 §8 仍保留 `agent-private/agent-shared` 的兼容读取。Auth 清单不复用这些旧行。
- 此前注册表缺少原生 Auth list；2026-11-14 新增独立元数据读方法，并非旧方法改名。
  `user.provider.list` 的返回是共享 Key，`agent.secret.list` 的返回是服务秘钥授权 grants，均不代表原生文件。
  前端已拆为 `listKeys()` 与 `listSecretGrants()` 两个独立读取；原生 Auth 继续独立读取，
  不恢复 `listCredentials()` 聚合，也不新增一个聚合 daemon 方法。
- 全局凭据设置用不带 actor 的受权限约束清单；单个 Agent 用 actor 筛选。
  条目包含已导入/缺失等可证明的文件事实及独立启动证据；不把进程运行等同于该 Auth 的厂商验证。
- 原生移除沿用已有 `agent.provider.logout`；前端按钮进展见第七节。它删除指定 Agent/harness 的本地原生凭据，
  不代表在厂商网站退出账号或撤销远端授权，也不保证抹去正在运行进程已经加载的凭据。

### 删除 GUI 专用 Lark 入口，保留平台实现

| 旧 GUI 专用入口 | 收敛到的统一方法 |
|---|---|
| `lark.adapters` | `adapter.list/status/pins` + `adapter.authorization.status`，由前端适配器组合展示 |
| `lark.authorization.status` | `adapter.authorization.status` |
| `lark.authorization.start` | `adapter.authorization.begin`，daemon 持有操作生命周期 |
| `lark.authorization.operation/cancel` | `adapter.authorization.operation/cancel` |
| `lark.binding.set/clear` | 已有 `adapter.pin/unpin`，保留 expectedActor 检查 |

清理范围包括 `gui/src/adapters/live/lark-controller.ts` 的旧调用、`gui/src/pages/live/LiveLarkPage.tsx` 及组合入口、
`shell/impl/gui/live/channels.py` 的旧业务分发及 backend allowlist。
无产品消费者的 withLiveAuthorization 和无效 channelContent 注入已移除；剩余消费者迁移后再删除，保留共用 client/业务模块。
保留 daemon 下 Lark SDK、授权服务、身份同步、入出站等实现；它们是 `adapter.*` 的平台实现，
不是应被删除的重复 GUI 业务。新链验收通过后再移除旧链，不能先删后留下不可用页面。

### CLI 分批迁移到同一 daemon 权威

1. 已经 `CLI → daemon request` 的方法继续复用原名、handler 和业务实现；缺声明的先补 MethodSpec/注册/HTTP。
2. CLI 直接调用公开库的平台注册、身份同步、路由等，先由 daemon 接管业务编排和操作状态，
   在所属模块补方法与 MethodSpec/注册绑定，复用现有库；不把 shell/CLI 实现反向 import 进 daemon。
3. GUI 经统一 HTTP 链消费这些方法；不能因为 CLI 暂未迁移就恢复 GUI 专用 `lark.*` 或直接调用库。
4. 接口与回执稳定、现有 CLI 参数/输出能够保持时，同批将 CLI 改为 daemon request，补 CLI/HTTP 行为一致测试。
   若 CLI 还有交互、离线执行或恢复语义未定，本轮先完成 daemon 能力，单列剩余调用点与迁移条件；
   不为迁移仓促删除 CLI 能力，也不让两条路径各建一份持久状态。
5. 最终移除迁走的 CLI 编排和 GUI 专用分发，只保留同一业务权威与共享实现；
   “daemon 已有方法”和“CLI 已全部走 daemon request”分开验收，不将前者写成后者。


## 七、Auth 前端效果与逻辑（2026-11-13）

当前共享前端与 mock 的行为如下；正式元数据读取已接入，导入与启动证据的剩余缺口见下方 T12b 增量。

| 操作 | 页面结果 | 现有或计划链路 |
|---|---|---|
| 创建时导入 Auth | 创建记录后立即显示在 Agent「凭据」与全局「凭据设置」的 Auth 清单 | 已有 agent.create → agent.provider.login；正式读取 agent.provider.auth.list |
| 全局查看/刷新 | 只读显示 Agent、harness、登录文件、已保存/文件不完整/读取失败、最近启动；不显示文件内容 | 当前为独立 nativeAuth.list UI 数据能力；正式已接 generated client → HTTP → MethodSpec / daemon request → agent.provider.auth.list → 原生文件元数据投影 |
| 详情查看 | 同一清单按 actor 筛选；没有正式读能力时留空，读取失败不显示零条 | 与全局同一读能力；不把 agent.secret.list 当 Auth，不因 Key 列表失败阻断独立 Auth 读取 |
| 显式启动 | 完成后自动重读清单；时间只属于当前导入版本的启动证据，不是厂商认证状态 | 已有 agent.start；不新增 auth.status 或验证方法，不根据 running 合成 verified |
| 编辑凭据 → Auth → 使用已有 Auth → 完成 | 重读当前 Agent/harness，记录存在即关闭；不要求上传，不改文件、Key 授权或启动关联 | 只读 nativeAuth.list；正式读方法 agent.provider.auth.list，无新增写方法 |
| 编辑凭据 → Auth → 上传替换 → 选择文件 → 保存 | 覆盖该 Agent/harness 的文件记录；清除旧版本启动关联；不删除 Key | 已有 agent.provider.login {actor,provider,files,force:true} |
| 移除 Auth → 确认 | 详情记录移除，再进入全局页也不显示；Agent、Key 与正在运行进程保留 | 已有 agent.provider.logout {actor,provider}；正式按钮需可信清单定位目标，不另加删除方法 |
| 移除回包不明 | 禁止重复移除，只可重新读取；确认记录消失后解除锁定 | 不重放写请求；未派发错误可明确重试 |

开发数据仅保存文件名、Agent/harness、导入时间、启动关联，不保留 Auth 字节或 token，不写 localStorage。
Pi 的 auth.json + models-store.json 为一条记录；Claude/Codex 各一条单文件记录。
模拟启动仅在未指定 Key provider 且存在匹配 harness 的原生记录时关联时间，启动失败、Key 启动均不能证明 Auth；
停止后保留该版本最近一次启动时间，重新导入清空，重命名即时投影，删除 Agent 同步清理。
真实实现仍需以可追溯的凭据版本和启动来源为证，不能照搬模拟时间作为正式验证结果。

共享展示：features/agent-provider/impl/native-auth.tsx；
全局 Key 与 Auth 都复用公开 CredentialList / CredentialListItem（impl/credential-list.tsx 与 credential-list.css），
Agent 配置页的 Key 与 Auth 同样复用；两类在详情页默认展开，均可收起/查看详情。
清单统一展开式卡片，详情桌面双列、窄屏单列；
纯前端预览与 dist-parity 使用相同组件，正式 adapter 不注入模拟读取。

已有 Auth 默认沿用；弹窗内切换 Key / Auth 保留选择和待上传文件。保存 Key 授权不删除已有 Auth，
重新打开仍可读取该记录。完成沿用前再次读取，若已被移除则停留弹窗并要求选择文件。
没有正式读能力或读取失败时不推断“已有 Auth”，保留显式上传/替换入口。
“使用已有 Auth”只表示沿用已保存的原生文件，不是新的认证切换方法，不自动撤销 Key；
启动凭据取舍仍受既有 provider / harness 启动规则约束。

Auth 清单读方法已实现；剩余后端范围为可证明的导入版本及启动关联元数据，不扩充新 Auth 写方法。
平台 Lark/adapter 收敛和 CLI 迁移按第六节后续处理，本节未改平台或 CLI。

## 八、T12b 正式 Auth 清单增量（2026-11-14）

- 调用链：共享 `NativeAuthList` / 编辑器 → `createSharedCredentialsApiFromClient.nativeAuth.list` → GeneratedClient → HTTP `agent.provider.auth.list` → MethodSpec 注册 → daemon `_handle_native_auth_list` → identity 公共 `inspect_native_credentials`。每次清单一次 HTTP 请求，后端枚举当前 owner 的 Agent，无前端 N+1，不调用 Key store。
- 参数：可选 `actor`、`harness`（claude/codex/pi）；actor 是业务目标，不是 caller。仅 local-operator，worker/session 不能枚举；全部清单过滤非 owner/host-invite，显式外部目标拒绝。
- 返回：`{ok, records}`；每条 `{actor, displayName, harness, state, files, missingFiles, errorCode, importedAt, startupConfirmedAt}`。文件仅 basename；读私有目录和文件元数据，不读内容，不返回路径/secret。stored 为所需非空 0600 文件齐全，partial 为缺失/空文件/残留写入，unavailable 保留脱敏错误码；无文件才省略记录。
- `importedAt/startupConfirmedAt` 正式始终 null：现有 writer 未持久化可追溯导入回执，现有运行观测也不能证明使用当前 Auth。mtime/Agent在线/模型响应不作替代；版本关联证据仍为 T12b/T12d 待办。UI 隐藏未知导入时间、最近启动显示 —。
- 导入、替换、移除复用 `agent.provider.login/logout`。stored 才能沿用；partial 可明确上传替换修复，不完整记录不能被沿用；unavailable 需要重新读取或显式替换确认。未知移除结果沿用既有只读恢复，不重放写。
- 已测：后端 RED 4 FAIL → 核心读取 4 PASS；扩展到 7 PASS，覆盖权限、外部 owner、partial、unsafe、空文件及真实 HTTP 三种 harness 写读回。既有 native writer/MethodSpec 焦点合计 65 PASS（新增权限边界两项另跑 7 PASS）。前端 RED 2 FAIL → 42 PASS；文案同步后凭据三文件 50 PASS。
- 真实浏览器：受控身份/设备、隔离真实 daemon/HTTP、production Node、Chrome；三种 harness 通过界面各完成创建→上传→详情→沿用（零额外上传）→替换→全局清单→移除。实际 agent.create 3、login 6、logout 3，1440/390 无溢出、无 JS 错误；合成文件，不是厂商认证/Agent启动成功。证据 `/private/tmp/gui-auth-inventory-e2e-qm6gwuxo/result.json`，不入库。
- 最终 GUI 2088 PASS / 12 FAIL / 2 SKIP，失败仅 KP-050；远端 GUI 与受控模型 E2E 通过。T10 U2、平台/路由与真实启动矩阵均未完成。
- PR hygiene 的本地合并树复现新增术语检查失败：Auth 方法的声明、示例、迁移登记与 import 共六处未登记。补充精确路径/整行 wire 名称，并覆盖新名称、附加内容和错误路径仍拒绝；7 FAIL → 20 PASS。不改产品行为，不增加目录通配、架构 baseline 或方法别名；不将本地根因直接冒充尚未取得的远端 job 日志。
- 完整 Python 在产品 `2348e7f56` 的隔离快照结束：27224 PASS / 43 FAIL / 45 SKIP / 7 XFAIL / 2 XPASS，无 ERROR。35 个失败与已在固定 dev 复现的集合逐项相同；其余为上述术语检查、六处方法/HTTP/示例清单断言及一个 golden 方法登记。只补新方法精确集合和 golden 未覆盖原因，不改业务或放宽断言。受影响八文件定向复跑 147 PASS；未重跑修后全量，不把两轮合成全量 PASS。原始日志 `/tmp/gui-auth-list-full.log`、归属 `/tmp/gui-auth-full-attribution.json`、回归 `/tmp/gui-auth-registry-green.log` 均不入库。
- 独立最终复审 `97e7966f7214392f18a9775ddfe620917dacd539`：限定范围 PASS。8/8 原失败项、受影响测试及术语 lint 140 PASS、IPC contracts 950 PASS、OpenAPI 42 PASS；产品行为、权限/目录安全、无 secret 泄漏及三种 harness 的受控真实浏览器流程均通过。产品代码仍为 `2348e7f56`；不代表真实厂商认证/启动、外部 issuer 或修后全量 Python 通过。
- 审核图 `wf-e8cf08b108394990bb8992d52e7c8198` 已先按 `9f90e683f` 写入 REWORK，未重置历史；最终增量的 PASS 见回执 `e56fbf59-18ff-5c7c-834e-d755cfacded4` 和更新后的 `/private/tmp/evidence-gui-auth-list-2348e7f-20261114/REPORT.md`。图输出与最终回执对应不同快照，不重复派单改写状态。
- 远端 `9f90e683f` 的 GUI、PR hygiene、声明及受控模型 E2E 均 PASS；Python checks/CLI inventory FAIL，分片未执行，具体远端 job 日志未取得。后续按 T11/T18/T19 推进平台；T10 U2 依赖保持 BLOCKED，T12b 启动关联证据并入 T12d 实测。

### T11 平台身份请求面（2026-11-14，F1 修复独立复核 PASS）

- 入口：`gui/src/features/connections/platform-integrations-api.ts` → generated client 的 `adapter.identities.list/sync` → `POST /daemon/request/<method>` → `request_catalog/adapter/identities.py` → `_AdapterAdminMixin` → 既有 `lark/credentials/identities.py`。不调用 CLI，不新建身份数据库。
- `list`：`{name, kind?}`；返回 `ok/adapter/count/identities`。kind 为 user/bot/app；仅当前应用命名空间；owner/unionId 只读既有值。查询不访问飞书。
- `sync`：`{name}`；返回 `ok/adapter/appId/bot/chats/chatsComplete/identitiesRecorded/errors`。`bot.appName`、`chats[].name` 均可为 null，沿用平台缺失名称，不编造名称；MethodSpec、OpenAPI、生成客户端与前端类型一致。保留原采集分页与 verified 绑定、source、firstSeen；本次记录数不是总记录数。总调度预算 6 秒、单个 SDK HTTP 请求 2 秒；预算耗尽停止发起新页面并返回 partial。SDK/存储调用仍可能超出总调度预算，HTTP 超时只能记未知，不能重放。
- 两者仅 local-operator，先检查调用者和已配置应用，再打开存储/访问网络；参数不能自报 actor/session。读取 retry=safe，同步 retry=never。平台异常文本不透出，启动失败映射脱敏 DAEMON_ERROR，已有记录可再次查询；HTTP-200 的 partial 保持 `ok:false`。
- 证据：RED 后端 6 项、前端 5 项失败；正确源码路径下后端/既有采集测试 35 PASS，GUI 23 PASS，typecheck/build、请求契约和 GUI 架构通过。真实 Chrome + production Node + 隔离 daemon 完成空读取→一次同步→部分报告→持久清单，1440/390 无横溢。受控身份/设备与飞书 SDK 分页，不是 Casdoor/飞书实网验收；全量和第三方结果待记录。原始证据在 `/tmp/gui-platform-*`，不提交仓库。
- CLI 迁移：现有 `shell/impl/cli/commands/adapter/data.py` 的 list/sync 仍直接消费 daemon 库；迁移时改为同名 request，处理现有离线 list、无总时限 sync 的兼容语义。当前新增的是 daemon 请求能力，未把 CLI 变成 GUI 的执行层；T20 跟踪，不新增平行业务实现。

本增量扩大回归：Python 1731 PASS / 1 FAIL / 1 ERROR，失败与清理错误均为同一用例 AF_UNIX 临时路径过长；指定 `/tmp/gpi-short` 后该用例 1 PASS。GUI 全量 2093 PASS / 12 FAIL / 2 SKIP，12 项均在 playwright-cache（KP-050）。CI scope/plan 156 PASS；Windows、结构/边界、请求契约、GUI 请求架构通过；daemon 请求架构 21 项与上一 Auth 检查点逐条相同，不增加基线。后端全量与独立审核仍待完成。

独立审核 `ecfe206993f97ec84b061de2397fd0c687d5cf40` 返回 REWORK：权限、应用隔离、分页/预算、脱敏、未知不重放及受控真实浏览器链路通过，唯一阻断 F1 是结果 schema 不接受 SDK 合法的空名称。修复只扩展两个可选名称字段的 null 类型，同步生成物并新增无名称示例；实际 HTTP 覆盖群名为空、机器人名称为空、两者同时为空，原实现 3 FAIL / 1 PASS。不增加方法、别名或 baseline。原审核证据 `/private/tmp/evidence-platform-identities-ecfe206-20261114/REPORT.md`；修后测试与复核另记，不能将原结论当作最终 PASS。

F1 修后验证：实际 HTTP 四种名称组合及身份/契约/HTTP 清单焦点 107 PASS；GUI 两文件 25 PASS，覆盖成功/partial 下的 null 名称。typecheck/build、client 生成一致性、请求契约、结构与模块门通过；修后第三方复核待定。正在运行的全量 Python 固定于修前 ecfe20699，不将该日志标作修后全量。

2026-11-14 独立复核 `ad135057d4e6556670ea815019620b53ef966a5e` 限定 PASS（图 `wf-6aa1cc60f0e7457f9c4c69fec3437698`）：实际 HTTP 13 PASS、扩大后端/契约 1303 PASS、GUI 焦点 30 PASS；受控空名称浏览器链路 PASS（一次 sync、两次 list、7 行/1 部分错误、1440/390 无横溢）。权限/采集器/预算/retry/baseline 不变；真实飞书、修后全量 Python 与远端 Python CI 仍未验收。证据 `/private/tmp/evidence-platform-null-ad13505-20261114/REPORT.md`。下一项为已有应用 adapter.register；CLI 只作为既有业务实现的查验来源，不作 GUI 执行通道。


### T11 已有应用注册请求面（2026-11-14，独立审核 PASS）

- 链路：共享 `PlatformIntegrationsSection` → `platform-integrations-api.ts` → GeneratedClient `adapter.register` → HTTP → `request_catalog/adapter/registration.py` → `_ipc_adapter_register` → 既有 `bootstrap/adapter_registration.add_lark_gateway`；随后 daemon 调既有 reload。无 CLI 子进程、无 Tauri 业务旁路、无第二份配置。
- 请求 `{provider:"lark",name,appId,appSecret}`；仅 local-operator，拒绝 body actor、force 和其他平台；HTTP readiness=lark-service、retry=never。未知结果只核对配置，不自动重放。
- 回执 `{ok:true,adapter:{provider,name,appId},configured:true,reloaded,nextAction}`。保存文件沿用私有权限、锁及事务回滚；不返回 secret/文件路径。保存不等于验证应用、批准权限、启动平台或绑定 Agent。
- 重名 `ADAPTER_EXISTS` 不覆盖；配置冲突 `ADAPTER_CONFIG_CONFLICT` 保留外部改动；无法确认写入结果用 `SUBMIT_OUTCOME_UNKNOWN`，异常文本脱敏。保存成功但 reload 失败返回 reloaded=false/nextAction=reload；GUI 提供手动全局 reload，即使列表为空仍可恢复。恢复列表读取失败不清除未知写保护。
- 用户操作：Agent 设置 → 添加接入 → 使用已有应用 → 填名称/App ID/App Secret → 开始添加 → 列表显示已配置接入。此注册快照中“创建新应用”禁用；后续 onboard 接线见下节；授权和启动分别进行，不伪造已授权状态。
- CLI `adapter add --app-id` 仍直接消费同一 daemon 库，未迁移；T20 后续改为 adapter.register 的薄客户端，保留显式 force/routes 等 CLI 旧能力的兼容评估。本增量不为这些 CLI 参数扩张 GUI 接口。
- 验证：后端 RED 11 FAIL→11 PASS，扩大到 190 PASS；GUI 四文件 38 PASS，含恢复读失败负例与 mock 重名语义。实际隔离 daemon/HTTP/production Node/Chrome：首次保存、同名拒绝、加载失败后显式恢复，3 register/1 reload/0 start，无 JS 错误，1440/390 无溢出。受控身份/设备、合成 App 凭据；真实飞书授权和整个配置→启动旅程仍 UNKNOWN。浏览器 `/private/tmp/gui-platform-register-e2e-vvnmdgky/result.json`、日志 `/tmp/gui-platform-register-*` 不入库。

注册增量回归：GUI 全量 2101 PASS / 12 FAIL / 2 SKIP，失败仅 KP-050；构建/typecheck、client/fixture/parity、结构/模块、Windows、方法登记/契约、GUI 架构、glossary、CI scope/plan 156 项通过。daemon 架构的 21 项与上一快照相同；未增 baseline/checker。源码工作树 Ruff 被未跟踪的历史 docs/notes 探针污染，交付使用干净固定快照复跑，不改历史材料。


### T11 平台权限读取（2026-11-14，独立审核 PASS）

- `adapter.authorization.status` 复用既有 `authorization/inventory.scope_inventory` → `configured_scope_client` → `diagnose_scopes`。daemon handler 先检查 local-operator 与已配置接入；不调用 CLI、不创建授权操作、不 apply scope、不启动平台。
- 参数 `{name}`；回执 `{ok:true,adapter,appId,state,capabilities,missingCapabilities,checkedAt,reason?}`。state 为 authorized/unauthorized/unconfigured/unconfirmed；checkedAt 为 Unix 毫秒。`ok:true` 表示已返回观察结果，不表示已授权。已授权能力和必需缺项保留后端 ID，不做中文映射。
- 查询权限的 token 获取和 scope GET 使用各自 2 秒 socket timeout；不是总耗时保证，慢流/文件 I/O 仍可能超过。CLI 和原授权服务默认仍 15 秒；异常由既有 inventory 脱敏，查询失败不当作无权限。
- GUI 列表仅 adapter.list；展开详情才独立读取权限，`刷新权限` 只做此读取。切换数据源拒绝旧回包；失败清除旧 grants，保留失败状态。该固定快照的授权按钮禁用；后续 begin/operation/cancel 增量见下节。
- 后端缺方法 RED 5 FAIL；现有 scope/授权库及新增请求 91 PASS，扩大后端/契约 256 PASS；新增实际 HTTP 与 socket timeout 7 PASS。GUI 五文件 40 PASS。隔离真实 daemon/HTTP/production Node/Chrome 添加应用→详情读取已授权/缺少项→手动刷新，两次 status、无授权写/启动/reload、1440/390、无 JS 错误。平台 scope client 受控，不代表真实飞书授权；结果 `/private/tmp/gui-platform-auth-status-e2e-69udfhsr/result.json`，日志 `/tmp/gui-platform-auth-status-*` 不入库。

权限读取增量回归：GUI 全量 2105 PASS / 12 KP-050 FAIL / 2 SKIP；统一准入复跑20 PASS，CI策略156 PASS，生成一致性/parity/build/typecheck、请求契约/方法登记、结构/模块/Windows/glossary/GUI架构通过。daemon架构21条与注册快照逐项相同，不增例外。注册远端GUI/声明/hygiene已PASS，Python checks/CLI inventory仍FAIL且分片未运行；fj只提供任务/状态，tea无登录，未取得job日志，远端根因不作推断。

### T11 平台授权操作（2026-11-14，独立审核 PASS）

- 三个方法经统一 HTTP/MethodSpec/daemon 链复用 `LarkAuthorizationService`、`reauthorize_lark_app` 和官方注册 SDK；不运行 CLI。daemon 持有 service/worker，关闭时拒绝新操作并回收所持进程。
- begin 参数 `{name,capabilities,operationKey}`；capabilities 必须明确包含 message-send、receive-p2p、receive-group-mention，可显式增加已有能力 ID，不默认申请 group-history。GUI 本轮只申请这三项基础能力，平台页面由用户批准。
- begin/cancel 为 retry=never，operation 为 safe；只授予 local-operator。operation 参数为 `{operationId}` 或 `{name}`；按 name 用于页面重新打开、begin 回包丢失时读回，不能以重新 begin 代替查询。
- 回执含 operationId、adapter/provider/kind、status、authorization，等待时才含 verificationUrl/expiresAt；checkedAt/expiresAt 均为 Unix 毫秒，URL 验证复用现有白名单。取消操作不代表撤销应用已有权限。
- daemon 在开始 effect 前将回执保存于私有 state/adapter-authorization/receipts.json，复用 kernel 私有原子文件写入；不保存 secret 或授权 URL。相同 operationKey 返回同一回执，参数冲突拒绝；重启后的未终结操作标 unknown，不恢复外部写入。最多128条回执，容量达到后明确拒绝，不通过静默遗忘操作键重放写入。
- 未知结果只读操作或刷新权限；新权限观察可以核对既有 unknown 回执，但仍在持有未确认 worker 时不标成功。持久化失败后拒绝新提交；取消仍须回收 worker。文件系统 fsync 无总耗时保证，现有 shutdown backstop 仍兜底，不能称硬实时关闭。
- 共享平台详情恢复最近操作；丢失 begin 回包后禁用重复提交，刷新只读 name/operationId；来源变更的旧回包不覆盖新状态。scope 状态独立读取，取消/失败不会清除实际已有授权。
- 作者验证：最初 service7 FAIL、request8 FAIL、GUI3 FAIL，修后后端/授权72 PASS；扩大后端/契约1588 PASS，1个Unix路径环境失败用短隔离目录复跑1 PASS。取消持久化失败另有RED→GREEN。受控真实daemon/HTTP/production Node/Chrome：登记已有应用→授权→刷新页面恢复→取消→再次授权→读回，2 begin/1 cancel，1440/390无溢出、无JS错误。没有真实飞书/issuer调用，不声明实网授权PASS。

权限读取独立复核固定7d9a6c938限定PASS：后端扩大1845、GUI40/全量2105 PASS，12项仅KP-050；受控浏览器及实际阻塞socket2.012秒通过。完整Python、真实飞书/issuer和远端CI仍UNKNOWN。证据 `/private/tmp/evidence-platform-auth-status-7d9a6c9-20261114/REPORT.md`。

授权操作前端全量：2106 PASS / 14 FAIL / 2 SKIP；12 项为既有 KP-050，另 2 项是新增三个方法后方法清单计数未同步，已按实际声明更新 114→117、142→145，保留逐项白名单断言。修后清单与操作 UI 焦点 8 PASS，最终 build PASS；不拼成修后全量 PASS。

独立复核固定6309ab75a限定PASS：后端72/扩大1827、GUI焦点43、回执7及GUI写探针2通过；GUI全量2108 PASS/12既有KP-050 FAIL/2 SKIP；受控真实daemon/HTTP/production Node/Chrome链2begin/1cancel、无JS错误。报告 `/private/tmp/evidence-platform-auth-write-6309ab7-20261114/REPORT.md`。非阻断观察：observe落盘失败时内存与回执短暂不同、配置换App后可能读到旧终态、journal浅层校验及前置错误保守显示unknown；未扩大本轮范围。完整Python旧ecfe为27246 PASS/40 FAIL，35项同固定dev，额外5项聚焦复跑已通过（shutdown 时序未复现、3项继承 Claude context window 配置、1项浏览器/隔离工作树依赖）；当前6309全量已启动，不记PASS。

### T11 新建平台应用（2026-11-14，待独立审核）

- 链路：共享 `PlatformIntegrationsSection` → GeneratedClient `adapter.onboard.*` → HTTP/MethodSpec → daemon `LarkOnboardingService` → 既有 `resolve_lark_app_credential`/官方 SDK → 既有 `add_lark_gateway` 和 reload。未调用 CLI，未引入第二份配置权威。
- begin `{provider:"lark",name,operationKey}`；operation `{operationId}` 或 `{name}`；cancel `{operationId}`。三者仅 local-operator，权限/参数检查先于 I/O；begin/cancel retry=never，查询 safe。operationKey 同键同名复用，异名拒绝；同名尚未确认的操作不会重新创建。
- 回执含 operationId、adapter/provider/kind、status、configured、reloaded、remoteOutcomeUnknown、nextAction，必要时含 appId/脱敏 errorCode；等待时才返回验证 URL 和 Unix 毫秒 expiresAt。configured=true 证明配置已保存，false 仅为尚未确认；reloaded=false 继续提供全局加载入口，成功不等于启动。
- SDK 创建在 daemon 持有的子进程中执行，结果文件私有原子写入；App Secret 只从 worker 传入既有配置写入，不返回页面或进入回执。回执在 effect 前落盘、128条上限不静默淘汰；URL 仅内存保存。重启时未终结记录变 unknown，不重放远端创建。
- 取消只停止等待并回收 worker，返回 remoteOutcomeUnknown=true；用户仍需在平台处理可能已创建的应用。远端创建后配置重名不覆盖；本地写结果不明用 APP_CONFIGURATION_UNCONFIRMED；落盘失败拒绝新写；worker 未确认退出不能标成功。文件系统 fsync 仍无硬总时限，沿用 shutdown backstop。
- GUI 同名表单先独立读取最近操作；恢复中/未知时禁止重新 begin。丢回包以 name 查询，来源切换拒绝迟到结果。已有应用模式继续只写 adapter.register；创建应用不自动发 adapter.start，不在浏览器编排注册与加载。
- 自测：新增保存不确定性用例先1 FAIL→修后通过；后端/既有授权焦点85 PASS，平台共享GUI49 PASS。真实隔离daemon/HTTP/production Node/Chrome完成 begin→刷新页面恢复同回执→cancel→显式第二次begin→受控SDK返回→保存/加载/列表读回，2begin/1cancel，0额外register/reload/start，1440/390无溢出、无JS错误。临时证据 `/tmp/gui-platform-onboard-*`；SDK与身份/设备受控，真实飞书/issuer未测。
- 单套GUI仍为上述共享组件，预览和正式模式只换数据源；生产产物无mock。Skill-Impact：gui-workflow已同步新平台操作路径，CLI迁移仍按T20另行处理。

新应用完整GUI回归为2111 PASS/12既有KP-050 FAIL/2 SKIP；build/typecheck/client/fixture/parity和结构、模块、术语、Windows、方法登记、请求契约、GUI架构、CI策略156项通过；daemon架构601/21，与6309逐条一致，未增加baseline。完整Python另在固定快照串行运行，结果不与焦点测试拼接。

新应用扩大后端/HTTP/生命周期回归2002 PASS，golden/OpenAPI70 PASS；原有应用注册与授权回归包含其中。第三方审核待固定提交派送。

2026-11-14 权限装配核对：cli-developer 在 dev@46a29ba48 确认 KP-030 所缺为生产 OwnerSpaceResolver→S3 owner-home 的 sub/owner 映射、provider record/grant 写入准入、U2 可信签发/绑定/发布/撤销及 composition 注入。现有 OrgFS/ACL、OwnerKeyPort 和签名原语应复用；只打开 HTTP 或 authority gate 不能补齐信任链。当前分支已按2026-11-10决定提前暴露HTTP，不恢复legacy Key兼容；已要求其按当前HEAD修正旧F-9建议。未接通项维持BLOCKED，属于T10待完成范围。

用户确认的个人权限边界（2026-11-14）：用户保存自己的 Key，在自己的 Agent 配置中选用；不增加组织审批、加入组织前置条件或额外 ACL 设置页面。`agent.provider.allow` 表达该 Agent 使用选中 Key 的许可，并不赋予组织管理权。现有 `provider_keys/context.py::is_local/resolve_agent` 仅接受同 owner 且非 host-invite 的本地 Agent；组织中可见不等于可修改，后续 move 也须按后端实际归属/授权判断，不能由页面自行解锁。本轮不扩展组织与 move。

KP-030 是上述个人操作所依赖的正式存储、身份与签名装配缺口，不是组织管理员未批准。cli-developer 已实读4cfc1b29a确认（回复1418d959-c5a7-5fdd-80b8-638694ee9307）：本人本机的新 Key 通路仍依赖 U2、owner-store 和写入准入，没有已批准且现存的个人正式替代通道；仓库及其可见 Forgejo 范围未发现正式 U2 发行实现，契约也未指定实现负责人。默认 legacy 旧方法仍有实现，但不满足本分支已确定的新方法路线；不静默切回旧方法，也不为缩短旅程另造权限体系。组织 Agent 的 provider.status 同样拒绝，不把目录可见写成凭据可读。

平台恢复入口修复：恢复 succeeded/reloaded=false 回执时沿用完成投影，空列表也保留全局reload；currentAdd读取失败有显式只读重试，失败期间禁止begin。仅前端变化，不新增方法或重放写；受控真实HTTP/浏览器已验证begin=1、reload=1。

全量回归更正（2026-11-14）：6309快照27285 PASS/47 FAIL；不能只按失败nodeid与dev相同判作继承失败。已定位并修复本分支新增的错误码字面量、严格resident清单漏项和shutdown测试替身缺少adapter操作资源的问题；收集阶段发现onboard/test_operations.py与既有测试重名，改为test_onboard_operations.py，不改收集规则。inbox检查误将泛称service.close识别为InboxService，已将明确的平台操作资源命名为operation_service，检查器和例外清单不改；实际InboxService同名别名仍被拒绝。聚焦48 PASS/2既有FAIL（external.py错误码字面量、windows_pty.py登记缺失）；后续全量另报，不拼接成PASS。

### T12f 工作目录启动接线（2026-11-14）

共享详情 → 启动弹窗 → GeneratedClient.agent.start({name,harness,provider,model,cwd}) → HTTP/MethodSpec → daemon `_ipc_agent_start` → 既有 agent.create(existing=reuse) → lifecycle.start。省略/null 保留 prior intent/Agent cwd/workspace 的旧解析；显式 cwd 必须是已有绝对目录，优先于旧 intent。复用 `validate_harness_cwd`，无效目录在写入前拒绝；已运行拒绝仍在原 create authority；有效设置在启动前保存，后续启动失败不回滚，不移动 home/凭据。无独立 cwd.set、无 CLI 子进程、无新权限面。

已完成 RED→GREEN（后端4项、GUI1项），后端启动文件28 PASS（含省略/null 的 AF_UNIX 用例）、GUI两文件62 PASS；扩大后端回归298 PASS/1 FAIL，唯一失败为未配置DEV01_CHROMIUM_EXECUTABLE；指定系统Chrome后该项独立1 PASS，保留原失败日志。全量GUI2125 PASS/12 KP-050 FAIL/2 SKIP，无新增GUI失败；类型、构建、结构/模块、方法登记、请求契约、GUI架构、生成一致性、Windows、变更文件Ruff通过；daemon架构输出与1e4979父快照逐字相同，既有失败未豁免。真实隔离 daemon/HTTP/production Node/Chrome 经过页面创建并导入合成Auth → 错误目录拒绝且原值保留 → 有效目录保存/独立读回并传入 lifecycle，1440/390 无溢出、errors=[]。启动端为受控 unconfirmed 回执，仅证明参数链和持久化，不是实际 harness/模型验证；证据 `/private/tmp/gui-agent-cwd-e2e-obj1u1go/result.json`。独立审核与固定提交全量仍待完成。

工作目录固定0e1b0b14e已同步普通远程分支和PR2727，独立审核图wf-2948f92daf1b4052832de10250e6f9db。干净固定checkout的全仓Ruff通过；本SHA完整Python排在1e4979完整运行之后，尚无结果，不合并两个快照的证据。

平台回归1e4979f955已获独立限定 PASS：确定性拒绝/未知结果区分、逐动作 MethodAccess、真实受控浏览器 add=2/remove=1 且无自动重放；GUI2124 PASS/12既有KP-050 FAIL/2SKIP，完整Python仍运行。报告 `/private/tmp/evidence-platform-regression-1e4979f-20261114/REPORT.md`。遗留的顶部刷新路由按钮能力投影在下一增量修复；checker 对 tuple/for provenance 的 P3 缺口不影响当前仅含平台操作服务的 close 元组，不扩大豁免。

删除接线核查（2026-11-14）：既有 `management.adapter.remove` 的文件/registry事务并不包含运行状态预约；caller 先 stop 再 remove 仍可能与 start 竞争。既有 `agent.destroy` 已有领域删除预约，但当前 preview/execute 尚未绑定。`docs/design/daemon-request/design.md` §7 和 cli-inventory 要求破坏操作 execute 只收预览 operationId；当前 HTTP catalog 又明确拒绝 confirmation!=none。后续需要补齐配对预览、领域内版本核对/认领和 HTTP profile，不能只标 http_exposed 或降为 confirmation=none 绕过。正在向 cli-developer 核对已有接点；方法清单待该核查结论更新。

cli-developer 已实读确认（e0075d83/7ba593cf 回复）：无现成正式删除/启动共享 reservation 或配对 HTTP 接点。实施沿既有 design §7：新增 `management.adapter.remove.preview`，既有 remove 与 agent.destroy 的 execute 只收 operationId，CLI --yes 也先preview；复用 PreviewRegistry，但目标版本校验/认领必须在领域串行化边界内。Lark actor 增加独立 removal reservation，阻断同名start/stop及reload/restore，先停进程，再配置/registry事务，最后更新actor快照并settle；不沿用会60秒自动释放的transition，不在actor mailbox做文件I/O。未知/未结算保持拒绝新启动，不能以超时释放造成删除与启动竞争。HTTP投影和catalog两处profile需同步扩展；原“16个拟新增请求”是初始清单，未含此次核实的删除preview，不能据此声称只剩user.provider.test。

T11 查询绑定接线验证：GUI43焦点、真实隔离HTTP/identity19通过；明确绑定查询4项及身份列表迟到结果1项先失败后通过。真实隔离daemon/HTTP/production Node/Chrome读现有本地绑定：已确认成员、仅出站访客、未绑定用户各一次，3次identity.resolve，无默认读取、App/bot无查询按钮，1440/390无页面横溢、errors=[]。身份与平台入站数据为隔离测试准备，非真实issuer/Lark验收；第一次探针仅有用户绑定、缺平台union观测时正确返回未绑定，补齐观测后通过，不为通过而改后端语义。证据 `/private/tmp/gui-platform-binding-e2e-mb3zvkh_/result.json`；原始日志不入库。

本增量全量GUI2133 PASS/12既有KP-050 FAIL/2SKIP；build/typecheck/module、结构/边界、GUI请求架构、术语、parity、CI策略156项通过。仅GUI/文档变化，无新增后端方法/契约/准入变化，未重新启动全量Python；已有固定后端SHA全量继续串行运行。本增量独立审核待完成。

T12f 独立审核完成：0e1b0b14e，agent320/inventory139/HTTP132、GUI62及受控浏览器PASS；全量GUI2125/12既有KP-050/2SKIP。报告 `/private/tmp/evidence-agent-cwd-0e1b0b14-20261114/REPORT.md`；完整Python、真实harness/provider仍未知。P3 参数拒绝文案未区分已派发，纳入后续修正。

### T18/T12e HTTP 确认协议基础（2026-11-14，独立限定 PASS）

HTTP catalog 与 OpenAPI 投影共用确认声明校验：preview 为只读且带有效期和绑定元数据；execute 为 retry=never 写，参数严格只有 operationId，禁止身份字段注入，配对预览缺失或不在 HTTP 面上时拒绝 execute。preview/previewOf 纳入声明与 HTTP 契约摘要，目录/OpenAPI 保留元数据。当前没有已开放 HTTP 的业务确认对，因此本增量不开放删除能力、不修改业务 handler 或 CLI。领域授权、可信会话绑定、版本锁、执行认领与未知回执仍在后续 T18/T12e 实现。

先失败2项后焦点76项通过（包含真实隔离 daemon/IPC/HTTP：预览、拒绝替换参数、重复确认仅执行一次、未知编号拒绝）；域删除及浏览器删除尚未测试，不能据此标记可用。扩大回归、完整 Python 与独立审核分别记录，原始日志在 /tmp/gui-delete-http-confirmation-*.log。

查询绑定3faf1c6独立审核发现同步完成后的旧闭包可覆盖新来源身份列表；c78bf582a已修独立sync代数和在途状态，新name/API来源不会接受旧sync读回或被其finally清除busy。4项RED→GREEN、3文件47 PASS、typecheck通过；复审图wf-c8e64744beaa4aa7ab9ad1c228ec4add。原FAIL保留，不把先前局部PASS扩展成整包PASS。

HTTP确认基础扩大回归345 PASS/1 FAIL；唯一失败为未配置DEV01_CHROMIUM_EXECUTABLE的浏览器入口，指定系统Chrome单独复跑1 PASS，保留原缺配置失败。结构/边界、方法登记、请求契约、生成客户端、术语、Windows、CI策略156项通过；daemon架构仍601/21，未修改checker/baseline。远端3faf的GUI与声明门通过，hygiene/Python检查及CLI inventory失败；最新dev64551e355已修部分基线门，合并树预检只有known-problems文档冲突，尚未据此认定远端全部根因或验收通过。

身份同步返工全量GUI2137 PASS/12既有KP-050 FAIL/2SKIP，新增四项均通过；固定提交c78bf582a，原始日志 /tmp/gui-platform-binding-sync-full.log。

2026-11-14 再次 rebase 到 dev64551e355：40个提交顺序重放，除known-problems追加与编号冲突外patch等价；产品GUI/HTTP文件与原固定头逐字相同。dev新增teleport生命周期与CI/CLI清单修复；daemon请求架构门现为PASS（base=64551e355），不再沿用旧601/21失败结论。原provider-key mock条目从分支KP047改KP058，保留上游teleport KP047。累计新HEAD的HTTP/lifecycle与完整Python另验，旧SHA报告仅保留其范围。

同步返工 c78bf582a 独立限定 PASS：47焦点、全量GUI2137 PASS/12既有KP-050 FAIL/2SKIP；受控真实daemon/HTTP/Node/Chrome主流程1sync/2list/3resolve、errors=[]。旧来源延迟竞态由独立jsdom探针RED→GREEN覆盖，并非真实浏览器竞态测试。父级同步通知/reload来源栅栏保留P3待修。报告 `/private/tmp/evidence-platform-binding-c78bf58-20261114/REPORT.md`。新基线累计 b7cb8aa92 的干净Python3.12/npm ci环境全仓Ruff通过，完整Python排队；旧cwd全量队列在启动前取消并由累计头替代，不记通过。

T18 领域基础：AdapterRuntime 增加 actor 持有的删除预约，独立于会过期的启动 transition；停止完成后保留，配置事务提交才移除投影/递增版本，回滚只解锁。启动/停止/reload/restore与在途生命周期互斥；超时、迟到回执和actor代际重启不会误解锁。原诊断字段显示持有时间，不暴露token；没有新HTTP方法或产品删除接线。4项先失败，7项完成后通过，actor回归25项通过；扩大焦点246 PASS/3 CLI树失败，单文件4项CLI失败在固定父b7cb同样复现，不为本轮改动扩大修复。完整Python与独立审核另记。

旧固定1e4979完整Python结束：27333 PASS/38 FAIL/44 SKIP/7 XFAIL/2 XPASS，未宣称通过。新基线累计b7cb完整Python已开始；本次actor预约尚不在该快照内，需在最终累计快照完整复跑。按用户关于Rust迁移的最新指示，只处理本分支引入/直接相关失败。

HTTP基础独立审核：旧1c2cea2（新基线b7cb8aa92），124条产品路由和摘要与父一致，未开放产品删除；12项新增协议测试、扩大345项及补配置后的Chrome入口通过。报告 `/private/tmp/evidence-http-confirm-1c2cea2-20261114/REPORT.md`。复审指出的HTTP隐藏operationId造成目录构建异常已补拒绝，新增测试先失败后通过；不改变有效方法的投影。

删除协议容量补强：PreviewRegistry不因过期、容量压力或discard遗忘执行中的claim；容量全部被执行占用时，在发出新预览前返回既有RATE_LIMITED，后续业务预览声明须列出该错误。已完成记录至少保留至原有效期，重复认领不重放写；跨daemon重启仍由领域恢复负责。3项容量/过期回归先失败后通过，连同HTTP隐藏字段修复与真实HTTP确认用例共25项通过；扩大回归1010 PASS/1 FAIL/1 ERROR，唯一失败源于macOS临时socket路径过长，短路径复跑该文件18 PASS，保留原失败。结构/模块、方法登记、请求契约、daemon架构、术语、Windows、变更Ruff通过。独立审核待固定提交，未作为产品删除验收。

cli-developer确认现有可信边界：daemon只收到IPC peer与可信origin，HTTP业务view/session不透传到领域。删除预览沿既有operator准入，从app.owner推导host principal、绑定app.epoch，session=None；同一host principal跨CLI/HTTP可以认领，不宣称逐浏览器会话隔离。请求体actor/sessionRef不能自报身份；不为本次删除另建身份机制。

78fbb5ef6领域预约基础独立限定PASS：Lark801、lifecycle/actors240、application853 PASS/1 SKIP；固定父对照确认pin/CLI的既有失败。报告 `/private/tmp/evidence-removal-lifecycle-78fbb5e-20261114/REPORT.md`。复审P3的完成回执恢复已由ed7f29修复并独立限定PASS：同一authority保留完成回执，下一个actor命令先按版本栅栏核对再准入，不重发停止、不凭过期解锁。独立27项探针、4项父版RED→GREEN、Lark805 PASS；报告 `/private/tmp/evidence-removal-recovery-ed7f29b-20261114/REPORT.md`。

删除/pin并发接点已由cli-developer实读确认（9f5f4fda）：现有management lease不覆盖直接agents.pin，值级expectedActor CAS不能保护整个删除区间；初始未绑定时commit甚至不执行unpin_if。产品删除开放前必须把pin与删除纳入同一authority原子准入，不能只读取lease标志或扩大用户权限。当前仍未开放业务删除，避免将内部基础可用误写为整条链完成。

内部pin预约已实现，方向由cli-developer按dev64551再次核对（b629652b）：typed reserve/release在Agent mailbox与pin写共用准入，reserve返回权威快照，不由外层另读。持有期间普通pin/unpin、目标Agent销毁及迁移拒绝；原生home在途恢复也不能绕过。token只允许strict CAS移除或恢复原Agent；P2 receive的最终pin改走Agent port，landing仍用原registry。容量128、不自动过期、代际共享、关闭需等待释放。本项未增加HTTP方法，也未把management事务、HTTP/CLI删除改成可用；跨daemon重启仍需领域按持久事实恢复，不能把内存预约当持久日志。

验证：4项初始RED；最终pin/P2焦点18 PASS；扩大首次906 PASS/5 FAIL/3 SKIP中的4项为继承会话环境、1项为新增测试漏填headless，隔离环境与测试参数修正后累计919 PASS/3 SKIP。结构/模块、方法登记、请求契约、daemon架构、术语、Windows和变更Ruff通过；未改门禁或baseline。证据 `/tmp/gui-pin-*.log`，完整Python与独立审核另记；本项不包含GUI/真实平台删除验收。

确认协议补强dd8c83be9独立限定PASS：25焦点、父版4项RED、扩大1298项及补Chrome配置后的浏览器测试通过，静态门和daemon架构通过；容量/过期保护与HTTP隐藏operationId拒绝均已独立复现。报告 `/private/tmp/evidence-preview-custody-dd8c83b-20261114/REPORT.md`。完整Python、产品删除领域认领和跨进程恢复仍未由此验收。

T18 管理事务接入：在线 BeginAdapterRemoval 先通过 Agent port 原子预约并取得 pin 快照；不再外层读取 pins。commit/rollback 使用 token 严格 CAS；回滚未完成、预约释放失败都保留事务与管理 lease，不重发原删除。取得预约但回执丢失时，仅对原 token 有序释放；确定未受理的拒绝不会释放其他预约。离线 handler 仍使用既有独占状态锁，本增量未开放 HTTP 删除、未替代文件补偿与跨 daemon 恢复。初始5 RED，最终管理层19、既有CLI删除27、扩大217 PASS；结构/模块/方法/契约/daemon架构通过。证据 `/tmp/gui-removal-transaction-*.log`，独立审核待提交后进行。

T18 运行时接点：`LarkPortClient.begin_removal/finish_removal` 经 frozen command 接到已有 AdapterRuntime mailbox，不调用 CLI。begin 在同一 mailbox 内核对运行代际和目标版本后才预约并停止；过期版本返回 PREVIEW_STALE且不停止，错误token/未结算停止不能提交。stop投影先发布再生成回执，避免停止成功回执仍显示运行中。9项新焦点及Lark814项通过；合成worker、隔离actor，不等于真实平台删除。尚需共享StateDatabase中的删除意图/阶段恢复、与配置及pin事务组合、正式preview/execute MethodSpec和GUI接线；现有公共HTTP面未增加删除方法。

T18 持久意图基础：`AdapterRemovalJournal` 借用 `app.state_db` 的短事务，在共享库中保存版本化删除意图，不创建独立数据库。原像包含channels精确字节、harness registration、legacy/Agent pin及Agent entity token；凭据只保存引用与摘要。prepared只能转compensating或committed；committed只可完成清理，不能转回补偿。相同operationId的输入绑定不可更换，同一目标只能有一条未结算意图；容量耗尽不淘汰未结算记录，终态回执保留。独立进程重读验证prepared/committed原样保留，损坏记录拒绝读取。本增量只是存储协议，不执行停止/文件删除、不在启动中调用，不代表跨daemon业务恢复已接通。

恢复组合约束（cli-developer按554260069实读确认）：在 `_startup_prepare_state` 的旧代际结算之后、`configure_user_adapters`及restore之前同步恢复；未结算不能开放启动。新恢复租约按journal原像而不是当前空快照补偿，核对Agent entity token，不能重用旧token或绑定同名新Agent。secret只可在持久logical commit之后按摘要删除；内部文件删除须接受已持有的管理事务，避免嵌套Begin死等。旧IPC删除入口应统一转入领域owner，正式开放时不得保留绕过路径。

恢复补偿入口已在既有管理/Agent port内实现：BeginAdapterRemoval可接受持久原像，Agent mailbox核对原实体身份与当前pin，保护暂时未绑定的恢复对象不被销毁或迁移；原像未绑定而当前已有pin时拒绝。新token只允许恢复原pin，不能转为正向删除或改目标；补偿失败保留租约，成功后显式释放。HarnessPortClient的snapshot/remove/restore复用已有HarnessRuntimeFacade异常投影，保留error_code/errno等不可变错误事实，避免I/O失败被当作已恢复。不新增RPC、不改变HTTP目录；正式文件/启动恢复与跨daemon业务验收仍待完成。2026-11-14先转回创建→凭据→启动→基础对话主线验证，删除后续不作为主线前置。

固定 b7cb 累计全量 Python 结果：27905 PASS/35 FAIL/44 SKIP/7 XFAIL/2 XPASS（`/tmp/gui-dev64551-python-full.log`），不记通过。dev64551 干净树对照确认既有错误码字面量及三项登录边界失败同样存在（24 PASS/4 FAIL）；网络可达性在 dev 通过、本分支失败，以及清理测试固定旧哈希，仍需本线修复。当前 ed7 全量继续运行，不包含后续 pin/事务增量；最终累计验收需另跑。

cfd6c159 独立限定 PASS 的补充缺口：lifecycle destroy 的补偿 create 会复用旧 payload 的 pinnedAdapters，绕过普通 pin 路径。管理事务接线前必须封住该路径；本线已增加 registry 只读有效补偿 pin 查询，由同一 Agent mailbox 在准入时与预约核对。包含 home 在途 claim 和非 home 补偿；拒绝时不产生 lifecycle 回执，释放后可用原 attempt 继续。两项真实 AgentActor/Registry 用例先失败再通过，连同管理层33 PASS；证据 `/tmp/gui-pin-revive-*.log`。这不等于跨 daemon 恢复或正式删除验收。

rebase 后两项本线检查修复：清理测试的固定种子摘要跟随 dev64551 已批准的37a293，不更改checker或ledger；平台 onboard 的注册回调按真实接点命名 `_register_adapter_configuration`，避免静态调用图把它错连至 ZenohTransport._register。仍指向既有 app 配置写入/加载方法，没有隐藏真实网络调用或增加例外。两项先失败，连同架构与 onboard 回归38 PASS（`/tmp/gui-rebased-architecture-{red,green}.log`）。补偿 pin 读取失败增加显式拒绝，最终 pin/管理焦点34 PASS；扩大与全量结果独立记录。
