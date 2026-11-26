---
name: gui-workflow
description: "GUI 工作方式入口：业务线分工、fixtures 规则与开工包见「业务线工作（当前）」一节；当前启动路径（hyprial gui）与最小原生壳的指针见「当前启动与原生壳」一节。"
---

# GUI 工作方式

## 当前启动与原生壳

- 启动只有一条路径：`hyprial gui`；`hyprial gui dev` 等价于 `hyprial gui --with-dev-tools --live-source`；
  开发工具层只在 `--with-dev-tools` 下提供；`hyprial gui start|status|stop|upgrade` 返回 `INVALID_ARGUMENT`。
  daemon 驱动下会话 host 在自己的进程内提供 `request_http`，`hyprial gui` 只起一个 Python 子进程；启动记录的
  `backend_pid` 为 `null`、`backend_endpoint` 为空串，只有 `contract-mock` 驱动另起 Node 子进程。
  参数、准入、启动记录与开发模式见 `docs/guides/gui-dev-mode.md`。
- daemon 先行：daemon 是唯一常驻后端。`hyprial gui` 在 home 的每种状态下都先确保 daemon（前提：home 目录已存在且为私有，否则先报 `GUI_HOME_NOT_INITIALIZED` / `GUI_HOME_UNSAFE`；无 owner 的 home
  启动 bootstrap daemon）；关闭 GUI 不停 daemon（用 `hyprial daemon stop`）；登录、登出时 principal 在原 GUI 进程内换绑，
  origin 不变。不要在 GUI 之前手工 `daemon run`，也不要把“daemon 没起”当作准入错误去绕。
- 最小原生壳在 `clients/native/`（`launcher`、`src-tauri`），打包在 `packaging/clients/`（见该目录 README 与
  `scripts/build/package-macos-shell.sh`）。壳运行 `hyprial gui --no-open --json --port 0 --stdin-stop`，只负责窗口、
  单实例与监督该子进程，不持有 GUI/backend/session/inbox 权威，也不启动或停止 daemon。
- 旧独立 GUI（`src/gui`）与旧 Tauri 外壳（`app`）已不在仓库里，不恢复；恢复范围与责任见
  `docs/design/gui-retirement-restore-ledger-2026-10-27.md`，设计与进度见 `docs/design/gui-single-launch-2026-11-14.md`。

## 业务线工作（当前）

根 `gui/` 组件化网页 UI 按四条业务线开发：`user-settings`（用户配置与 daemon 管理，黄佳佳）、
`agents`（Agent 创建与管理，林懿伦）、`chat-channels`（聊天、消息与渠道，李震宇）、
`org-members`（组织、邀请与成员，姚升悦）。分工、开工流程、后端对接原则、能力覆盖矩阵与各线
后端状态表见 `gui/docs/business-lines/README.zh-CN.md` 及同目录各线文档。

fixtures 规则：单元测试专用 fixtures 放在 `gui/tests/fixtures/<slug>/`（slug 即业务线名）；
开发预览与测试共享的组合数据、mock 和 fixture state 放在 `gui/src/dev/`。正式源码不得 import
`gui/tests`，feature 公共入口不得导出 mock/fixture。各方先按自己的理解补全上下游 fixtures，文件名带
本线 slug 前缀以便整文件替换；开发时用自产 fixtures，不接真实状态；最终提交集成分支前与相关线比对，
用已完成方的实现 cherry-pick 替换自产版本（例如 org 相关 fixture 以 org-members 线为权威方）。

真实首次登录调试必须使用显式 `daemon` driver 与空隔离 home；初始化切换与登录登出时的行为
以 `docs/guides/gui-dev-mode.md` 的“Login, logout and the daemon”一节为准。
`npm run dev` 的 fixture 成功、Contract Mock 成功和真实 issuer 账号授权分别记录。
未知初始化回执只能刷新核对，禁止重发。初始化/退出时保留原 GUI 窗口，principal 原地换绑后经既有
control/view proof 刷新业务 session；旧业务 session 必须失效，不得自动重放写入。授权网站仍单独打开，完整原生壳验收需另外实测。

旧 Web `gui/server/auth`、`setup`、`live`、`daemon-request`（Node host，2026-11-16）及其 Python child
bridge 已退役；浏览器会话与 `/daemon/*` 只由 Python 会话 host（`src/hyprial/shell/impl/gui/host`）提供，
`--live-source`（`hyprial gui dev` 含它）在它之后放 Vite dev server（`npm start` 已删除）；开发工具层与 API 文档只在 `--with-dev-tools` 下由该 host 提供；
不要用 `/__gui/auth`、`/api/gui/v1/live` 或 `run_gui_backend` 接线。
四步引导前端、归档场景与原生 capability 保留；普通产品仍经 GeneratedClient → HTTP →
MethodSpec → daemon request。首次设置后续能力复用此链补齐，不恢复旧 Web worker。

Agent 凭据与创建调试以 `docs/design/gui-v0.1-method-plan-2026-11-13.md` 为统一对账：
创建只写记录和 Key 授权/原生 Auth 文件，再由用户配置模型并显式启动。不要恢复自动启动，
也不要用 `agent.secret.list` 冒充原生 Auth 清单。Auth 导入回执不证明厂商登录有效；
自定义地址、共享 Key 正式装配和真实模型响应按各自证据验收，mock 成功不能代替。
执行进度见 `docs/design/gui-v0.1-lifecycle-todo.md`。
Auth 清单读能力未注入时不伪造记录；已有记录可沿用，只有明确替换才重新上传，移除复用既有
agent.provider.logout。Key/Auth 清单及空状态共用展示组件，沿用前重读，不把沿用当认证切换。
凭据设置的“添加连接”选择 daemon 返回的官方地址预设，或填写自定义 Base URL、名称和 Key；
当前新连接限 OpenAI Chat Completions。模型 ID 在测试时手填，不随连接保存，不从预设推断。
测试使用既有 provider-test（30 秒探针、35 秒外层预算）；取消、关闭或离页中止本次请求，
成功只证明该次模型探针，不证明 Agent 能启动。保存未知或保存后回读失败先刷新核对，不重复提交。
新连接换 Key 必须携带已读 revision 并核对下一版本回执；使用该连接的 Agent 需重新配置。
声明了 agent.model 方法的入口复用同一 Agent 表单：选择 harness、连接/Key 或原生 Auth，
手填完整模型 ID。连接创建一次提交显式 modelConfiguration；原生 Auth 先创建带必需配置标记的身份，
再导入该 Agent 的登录文件、明确选择目标并保存，不能自动选择第一个目标或沿用默认 seed。
详情读取 status，编辑/取消携带读到的修订与操作 ID；prepared、清理中或读取失败时拒绝启动。
保存或创建结果未知只读核对；已返回身份的未完成创建从同一身份恢复，不重复创建。
启动只复用已保存模型配置，用户仍显式确认 cwd 与 Pi approval，不再选择 provider/model；
首轮引导也复用此流程并由用户显式启动。尚未声明新方法的旧入口保留旧流程，不能把新连接伪装成 DeepSeek Key。
原生 Auth 清单/导入与现有凭据探针保留；导入成功、探针成功、Agent 运行与模型响应分别报告。
创建不自动加入聊天列表；用户选择“开始对话”后展示，已有真实会话仍从后端读取。
停止后的工作目录修改随 `agent.start.cwd` 一次保存并启动，填写已有绝对目录；
省略时保留原目录解析。无效目录在写入前拒绝，后续启动失败则保留已保存设置；
不移动 Agent home 或凭据，不增独立 cwd.set，目录选择器仍待可信宿主接入。
Pi 在同一 `agent.start` 请求显式传 `approve=true` 与绝对 `cwd`；页面由用户确认，
重开或改目录清确认；daemon 严格解析并保存规范目录。批准随启动配置持久保存供自动恢复，不称一次性；false 在写入前拒绝，
省略兼容旧调用，false 不撤销历史批准；停止关闭自动恢复。不得默认批准、写 create.harnessArgs 或绕过 Pi 目录检查。

Agent 删除先调用 `agent.destroy.preview({name})`，展示工作区、绑定、例行任务与待处理消息，
用户确认后只提交 `agent.destroy({operationId})`。取消不提交 execute；过期/实体或绑定变化时重新预览，
未知结果先读回，不自动重复删除。操作 ID 绑定当前 daemon/session 与 Agent 实体；不从浏览器传可信身份。
CLI `hyprial agent destroy NAME --yes` 同样先取得预览，`--yes` 只省略交互确认；旧 name execute 已退役。
共享弹窗位于 `features/agents/impl/destroy/AgentDeleteDialog.tsx`，能力缺席不显示删除入口，
不要恢复旧 fixture 的直接删除 fallback。预览统计不是冻结的文件快照，已消费历史不在删除范围。

已有飞书应用从共享平台接入表单调用 `adapter.register`，不调用 CLI。保存与加载结果分别
显示；保存成功不等于授权或启动。未知写结果禁止自动重发，显式 reload 后仍须成功读回列表；
首次保存但加载失败时保留全局 reload 入口。新应用走 adapter.onboard.begin/operation/cancel，
daemon 复用官方 SDK 创建与既有配置写入；页面重开按接入名称恢复回执，未知结果只查询。
取消不删除可能已创建的远端应用；不把创建成功当作接入已启动。平台授权走 adapter.authorization.begin/operation/cancel；
仅显式批准的能力进入请求，刷新/回包未知只读操作，不重新 begin；取消不表示撤销平台权限。
平台删除复用 `management.adapter.remove.preview` → `management.adapter.remove`，执行只传 operationId；
运行中先显式停止并重新预览。确认覆盖绑定和路由，只删除本地接入，保留 Agent 与远端应用。
取消无副作用；执行未知不重发，关闭后刷新核对；读回列表确认消失后才显示删除成功。
共享/外部凭据保留，daemon 启动时恢复未结算删除，不重复执行平台停止。
平台权限通过 `adapter.authorization.status` 在详情中独立读取；列表不逐应用查询。
读取失败不能显示成没有权限，能力 ID 原样展示；此读取不发起授权或启动平台。
平台身份表的用户行可显式调用 `identity.resolve` 查询绑定；有 unionId 用联合身份，
否则用 adapter/openId。不对 App/bot 推断创建者，也不把仅出站绑定当已确认身份。
Agent 平台绑定中的消息路由走 `adapter.routes.list/candidates/add/remove`，候选来自实际入站观测。
写入携带读取所得 `expectedRevision`，默认项删除须明确 `clearDefault`；未知回包只读核对，
保存成功与加载成功分开。预览数据只在 dev 中维护；pin 为入站绑定，route 为命名外发目标。

根 `gui/` 的 13 个业务模块现已统一使用显式 `index.ts` 公共面和承载实际实现的 `impl/`；
不提交空 `.gitkeep`，跨 feature 不穿透 `impl/`。`settings` 是 `user-settings` 的现行源码目录，
`dev/` 是开发预览、mock 与共享开发 fixtures 的现行根目录。`npm --prefix gui run check:module-boundaries`
及 `gui/config/module-boundary-baseline.json` 是结构例外的事实源；`App` 组合根等当前例外只减不增，
收尾分工见 `docs/design/gui-deep-module-structure-2026-11-08.md`。

## 仍然适用的通用规则

- GUI/adapter 只能投影 daemon 持有的业务 session、inbox、turn 和 PAC 状态；不要从 UI、聊天记录或 mock 推断业务完成，也不复制 daemon 的投递/恢复权威。
- 新增入站消费路径须遵守 daemon ACK 契约：只有真实消费/注入成功才 ACK，拒绝或失败不误 ACK。
- Cordis 插件是受信任的本地代码，不是任意不可信插件的 sandbox。
