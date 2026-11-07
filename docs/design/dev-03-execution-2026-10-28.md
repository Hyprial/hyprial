# dev-03 执行记录与共享接口提案

状态：李震宇已进一步确认当前阶段开始前端开发，并使用 dev-mode 预览。按 [前端实施计划](dev-03-frontend-plan.md) 新增私有受控组件、测试与说明；冻结文档及共享接入仍待集成人确认，不扩展到共享入口或后端。

## 已确认约束

- 工作树：`harness-bridge/.worktrees/dev-03`；工作分支：`feat/gui-dev-03`。
- PR 目标仅为 `intg/gui-init`。开工刷新后，HEAD、`origin/feat/gui-dev-03`、`origin/intg/gui-init` 均为 `e703c6690b3bbcd16f558cb7293503d3c4208bb2`。
- 任务来源：`docs/guides/dev-03-development-prompt.txt`；用户已明确将其中“仅准备”的状态更新为“开始执行”。范围与权限遵循 `docs/design/gui-init-tracks.md` 第 6、9、10、11、12 节。
- 每完成一项可验收工作，通过李震宇的 Squire 通知结果及未完成边界。接收者：`agent:jo.carter@example.com:usrb-dev:squire`；消息以获授权的 `user:jo.carter@example.com` 发出，不借用其他 agent 身份。
- 最新授权允许启动指定 dev-03 worktree 的开发预览；不以生产 daemon/飞书 App 做功能测试。浏览器验收使用隔离 fixture。
- 未接通的发送和授权不发业务请求，显示未连接、状态未确认或暂不可用；模拟状态仅用于隔离测试。

## 分项进度

| 项目 | 当前状态 | 完成证据或依赖 |
| --- | --- | --- |
| 1a. 工作区与依赖准备 | 完成 | 正式分支及三个 ref 一致；Node 24.20.0 锁定安装成功，104 个包，审计 0 vulnerabilities；业务源码未改动 |
| 1b. 能力矩阵、候选私有文件清单与共享接口提案 | 初稿完成，待集成人确认 | 见 [完整提案](dev-03-interface-proposal.md)；14 项能力矩阵、8 项共享请求、14 个验收场景；冻结交接包与集成人网络身份已向 Squire 请求 |
| 1c. 基线门禁报告 | 完成；整体门禁未通过 | typecheck、146 项单元测试、production-copy、safe-r1-build、结构门与模块边界门通过；浏览器路径不兼容导致两项命令失败 |
| 1d. 公告原文核验与 daemon 方法映射 | 完成初稿 | 固定公告与公共消费基线已读取；[方法表](dev-03-daemon-method-map.md) 记录真实旧方法、身份/参数/错误、重试/幂等及缺口；未执行真实业务验收 |
| 2. 聊天流程 | 私有组件已实现，共享接入待完成 | 输入、列表/消息/信息区、等待/未知/草稿、键盘和响应式；共享状态接线由集成人负责，后续证据见前端计划 |
| 3. 飞书四步与渠道状态 | 私有组件已实现，真实业务未接通 | 选择 Agent、授权、切换渠道、结果的受控呈现；依赖 dev-02 标识和已验证请求合同 |
| 4. 完整验证、独立审阅、PR | 组件交付完成，整体门禁未通过 | PR #2062 目标 intg/gui-init；最终命令、浏览器证据及审阅见前端计划；共享浏览器门禁存在平台失败 |

## 代码事实与待交付能力

| 能力 | 当前实现 | 差距与归属 | 验证要求 |
| --- | --- | --- | --- |
| 会话与信息面板 | `gui/src/pages/ChatPage.tsx` 有会话列表、输入区、信息区和按会话滚动位置 | 精确私有文件边界待冻结包确认 | 会话切换、焦点返回、375 中心命中并点击 |
| 草稿保留 | `gui/src/state/appReducer.ts` 的 `send-message` 在请求结果前清空草稿并加入 `local-*` 消息 | 全局 reducer 属于 work-developer；需改变提交/回执时序 | 失败与未连接均保留原草稿；切换会话不串草稿 |
| 请求就绪与重复提交 | ChatPage 调用 `useStubAction.run('chat.send')`；`useStubAction` 只有局部 busy | 缺少已验证后端就绪证据；共享 adapter/runtime 接线由 work-developer 修改 | 未就绪业务请求数 0；忙碌不重复提交；未知结果不重发 |
| 消息历史与去重 | `gui/src/state/messages.ts` 有按 ID 合并、快照和 local 对账 helper | helper 不是正式历史/事件接线；共享状态不得复制到页面 | 真实权威 ID 对账、重连不重复、受理与回复区分 |
| 飞书渠道 | `gui/src/pages/ConnectionsPage.tsx` 与模型区域同页，动作仍是 `connection.adapter.*` 占位调用 | 共页拆分和公共入口先由集成人确认；本 track 不改模型区域 | 四步独立状态、未授权未连接、失败保留选择 |
| daemon 底层调用 | `src/hyprial/shell/impl/gui/adapter/chat.py` / `host.py` 提供既有映射 | 不证明浏览器链路 ready；不把 demo request 三步验收等同于 daemon request 源码存在 | 获取林懿伦/姚升悦核验记录或黄佳佳标准版本 |
| 浏览器门禁 | `gui/tests/dev-overlay-interaction.mjs` 和 `safe-r1-browser-contract.mjs` 硬编码 macOS Chrome 路径 | 共用测试仅由 work-developer 修改；当前主机为 Linux/WSL | 请求跨平台浏览器定位能力或精确提交的 macOS 验证；不得假造路径规避检查 |

## 共享接口提案 v0（待集成人确认，非已发布契约）

1. **聊天输入与提交**：沿用共享草稿与 target ID，不在私有组件复制会话业务状态。集成人提供就绪/未就绪原因、当前发送操作及提交/查询结果回调。输入内容只在已确认的成功条件满足后清理；明确失败保留并允许按合同重试，未知结果仅允许查询原请求，不产生新请求 ID。
2. **消息展示**：消费共享选择器提供的会话与权威消息，不由组件生成成功回执、历史或回复。纯展示组件的 `pending/failed/unknown` 映射遵已有 `operation-lifecycle.ts` 与已验证业务合同；不自行定义网络协议。
3. **飞书四步**：dev-02 提供 Agent 列表和稳定标识；集成人提供选择、授权、渠道切换、读取结果的正式能力及只读状态。每步的前进条件来自实际返回，返回上一步只改变临时 UI 导航，不撤销或伪造后端状态。
4. **渠道状态输出**：dev-03 提供视图投影与文案建议，work-developer 接入共享选择器供 dev-02 消费。至少区分未连接、状态未确认、暂不可用；只有契约证实的连接才能显示已连接。
5. **文件接入**：请确认 ChatPage 的私有拆分目录、独立测试文件，以及 ConnectionsPage 的渠道区域提取位置。组件公共导出、全局样式、AppShell 路由与挂载、adapter/runtime/reducer 改动由集成人提交。
6. **门禁可移植性**：请为共用浏览器脚本提供有验证的 executablePath 配置或跨平台发现规则；本 track 的私有测试可使用明确指定的本地 Chromium，但不将其冒充原有硬编码门禁通过。

这份提案只列需求与语义，不授予 dev-03 修改共享文件的权限。冻结交接包未取得前，不自行扩大私有目录范围。

## 协调记录

- 已通过 Hyprial 向 Squire 请求 `work-developer` 的准确 URI、三个冻结文件的位置/版本、当前 dev-03 是否已有其他执行者，以及 demo request/替代标准的就绪证据。
- 开工消息：`817d06e3-19b0-5b88-acfb-6fcaf26d35f4`；conversation：`2ce3ed2e-dce9-426f-904b-7292db18d4ad`。
- 已查询到 `fetched / ACK_RECEIVED`，证明接收者领取；不等于依赖已经解决。
- 第 1a 项完成通知：`fb41eca6-7765-579a-beb5-983c94eae494`，已确认 `fetched / ACK_RECEIVED`。
- 第 1b 项分析/提案初稿通知：`6adb5500-1c5f-5d25-9261-30b04e452788`，已确认 `fetched / ACK_RECEIVED`；请 Squire 协调 work-developer 确认文件边界与共享接线。
- 第 1c 项基线报告通知：`5cea07af-9c43-567e-8cdd-294d008f0115`，已确认 `fetched / ACK_RECEIVED`；明确整体门禁未通过。
- 至本次记录更新，用户消息 inbox 仍无依赖答复，Squire 工作区未发现所请求的冻结文档或回复文件。领取消息不等于已经转达用户或批准接口。
- 用户补充 cc-h2b-monitor 三段公告后，已通过内部 SSH 的 Git 对象取得 `a209031c6` 固定版本公告、公共消费基线和契约 README，核对公告内容一致，并读到 `origin/dev@f165ed0f2` 后续迁移方案。来源与对本组的约束已加入接口提案；具体 daemon 方法映射已落盘。没有合并这些上游分支或改动本组基线。

## 基线实测结果

以下检查针对原始集成基线的业务代码。本批只新增执行记录、接口提案和 daemon 方法表。没有把这些结果当作新功能验收。

| 检查 | 实测结果 | 证据 |
| --- | --- | --- |
| `npm run verify` | FAIL，退出 1 | typecheck 通过、24 个测试文件/146 项测试通过；test:browser 的 5 个视口全部因浏览器路径不存在而失败；后续链式步骤未执行。日志 `/tmp/dev03-baseline-verify.log` |
| `npm run verify:production-copy` | PASS，退出 0 | 单独补跑生产构建和文案门禁，0 forbidden markers；日志 `/tmp/dev03-baseline-verify-production-copy.log` |
| `npm run verify:safe-r1-build` | PASS，退出 0 | 单独补跑 development/production 构建契约；日志 `/tmp/dev03-baseline-verify-safe-r1-build.log` |
| `npm run verify:safe-r1-browser` | FAIL，退出 1 | 启动前要求 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`，当前主机不存在；日志 `/tmp/dev03-baseline-verify-safe-r1-browser.log` |
| `check_module_boundaries.py` | PASS | 输出 status PASS，1074 个文件、10672 个 import、12 个动态调用，violations 为空；静态证明不代表运行时接通 |
| `check_structural_limits.py` | PASS，退出 0 | 首次输出过大且与下一条命令混合，单独复核确认 status PASS、violations 为空；完整输出 `/tmp/dev03-baseline-structure.json` |
| `git diff --check` | PASS，退出 0 | 初次仅 untracked 文档，新增文档另查空白格式，并在提交前执行暂存差异检查 |

浏览器检查失败发生在启动前，未执行实际页面点击，也没有 375/760/1440 截图或零业务请求的浏览器证明。本次未修改共用脚本、伪造平台路径或使用生产服务替代测试。

提案已经过独立只读审阅，补充了 1440 下四个聊天区域完整显示的明确断言。该文档审阅不是业务实现验收。

## 验证方法

- Python 结构检查使用 `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 和 `check_module_boundaries.py`。
- GUI 使用 Node 24.20.0。当前 WSL 验证使用进程隔离的 loopback namespace，避免改动主机路由或日常服务。
- 必须如实记录 `npm run verify`、`npm run verify:production-copy`、`npm run verify:safe-r1-browser` 与 `git diff --check` 的结果；共享基线失败不改成跳过，也不记作业务实现回归。
- 本节是最初分析阶段的基线记录；后续组件实现和验收以 [前端实施计划](dev-03-frontend-plan.md) 及 [接入说明](../guides/dev-03-components.md) 为准。仍无真实业务全链路或 PR 可合并结论。
