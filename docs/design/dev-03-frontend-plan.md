# dev-03 前端实现计划

> 执行方式：subagent-driven-development，按任务测试、审阅并通知。

**Goal:** 交付可独立验证的聊天和飞书受控组件，供公共入口接入。

**Architecture:** 页面组件只消费 props 并发出用户意图，不创建第二套业务状态、网络适配器或后端契约。采用当前公共基线的 React、Button、Stepper 和 OperationStatus。开发预览显式指向 dev-03 worktree；隔离测试演示不进入生产入口。

**Tech Stack:** React 19、TypeScript、Vitest、Testing Library、Playwright、Node 24。

## Global Constraints

- 仅在 `.worktrees/dev-03` 的 `feat/gui-dev-03` 开发；PR 目标 `intg/gui-init`。
- 用户已确认本阶段开发前端。新增私有组件、测试和说明；不修改 App.tsx、AppShell.tsx、package.json、dev-module-index.css、共享 state/adapter/runtime、全局 CSS、共用测试或构建配置。
- 冻结归属文档仍未取得。当前授权不扩大到现有 ChatPage/ConnectionsPage；正式接入交给 work-developer，不能称全旅程已接通。
- 组件不 fetch、不读写凭据、不持久化草稿、不生成消息或操作权威 ID，不使用占位 501 作为可用接口。
- 后端未就绪默认不可用、业务回调零调用；未知结果禁止重新发送；只有控制器提供的权威结果可以显示成功。
- 草稿为受控前端输入：组件不在提交、失败或异步结果时自行清空；共享控制器负责按修订处理成功清空和跨会话存储。
- 复用现有公共组件和 OperationStatus，不添加依赖，不合并其他功能分支。
- 样式仅作用于新增组件；1440/760/375 无根横向溢出，375 信息关闭按钮中心可点击、焦点返回。
- 每完成一个可验证任务，通过 Squire 通知用户并核对 Lark 实际发送凭据；失败如实记录。

### Task 1: 受控聊天输入

Files: `gui/src/pages/chat/ChatComposer.tsx`、`gui/tests/dev-03/chat-composer.test.tsx`。

接口：导出 ChatComposer 和 ChatComposerProps。props 为 targetId:string|null、targetName:string、draft:string、capability:'ready'|'unavailable'|'unconfirmed'、operation?:{targetId:string;requestId:string;status:OperationStatus;canRetry:boolean;error?:string}、onDraftChange(targetId,value)、onSend(targetId,text)、onRetry(requestId)。回调只返回 void；控制器同步投影 pending。不添加查询按钮或后端协议。

- [x] 写失败测试并记录 RED：未连接时允许保存输入但点击/Enter/直接 submit 均不发送；空白不发送；中文 composition Enter 不提交；Shift+Enter 换行；正常 Enter/按钮提交保留原草稿；等待禁止重复；明确失败保留文本且只有 canRetry 为真可重试原 requestId；unknown 禁止发送/重试；不匹配 targetId 的旧操作不能作用于当前对象；可空对象不崩溃。
- [x] 实现语义化 textarea、Button、状态提示；原样保留输入，不主动清空。作用域样式由 Task 2 提供。
- [x] Node 24：`npx vitest run tests/dev-03/chat-composer.test.tsx --maxWorkers=1`，`npm run typecheck`，`git diff --check`。
- [x] 独立审阅、修正；记录证据后通知。

### Task 2: 聊天工作区

Files: `gui/src/pages/chat/ChatWorkspaceView.tsx`、`gui/src/pages/chat/chat.css`、`gui/tests/dev-03/chat-workspace.test.tsx`。

接口：导出受控 ChatWorkspaceView/props，消费 ChatComposer。复用现有 ChatTarget/ChatMessage 类型（核对后引用，不能虚构现有类型名）；受控 targets/activeTargetId/messages/draft、发送状态和回调；历史 loading/error/ready、信息展开与回调。允许局部 ref 保存滚动和焦点；不拥有业务 store。

- [x] 测试空 targets、失效选择、加载/失败/空历史，受控 A/B 草稿、按对象滚动、晚到消息不打断阅读、信息关闭后焦点回到 toggle；消息内容只来自 props。
- [x] 实现列表/消息/输入/信息区，复用 AgentAvatar/AvailabilityBadge；管理 Agent 不触发占位业务请求。
- [x] `npx vitest run tests/dev-03/chat-workspace.test.tsx tests/dev-03/chat-composer.test.tsx --maxWorkers=1`、typecheck、diff check；独立审阅后通知。

### Task 3: 飞书四步受控视图

Files: `gui/src/pages/channels/LarkChannelView.tsx`、`gui/src/pages/channels/channels.css`、`gui/tests/dev-03/lark-channel.test.tsx`。

接口：受控 Agent 选项/选择、步骤 chooseAgent/auth/switch/result、每步 OperationStatus、授权状态、当前 Agent 的权威渠道结果及能力；回调选择/步骤/授权/切换/明确失败重试。复用 Button/Stepper。返回保留选择；不创建凭据输入或 OAuth URL。

- [x] 测试四步独立状态、返回保留选择、未授权/过期/撤销未连接、unknown 不重发、未接线零回调、授权成功不等于已连接、结果须匹配当前 Agent、空 Agent 列表和键盘操作。
- [x] 最小实现和作用域响应式样式；typecheck、定向测试、独立审阅后通知。

### Task 4: 开发模式与交付验收

Files: `gui/tests/dev-03/preview/`（隔离浏览器测试 fixture，不是生产路由）、`gui/tests/dev-03/chat-browser.mjs`、`docs/guides/dev-03-components.md`，本计划记录结果。

- [x] 用 `hyprial gui dev --gui-home /mnt/d/workspace/hyprial/harness-bridge/.worktrees/dev-03/gui --no-open --ip ::1 --port 4174 --json` 启动，核对输出目录；有本机网络问题使用隔离测试实例并如实报告用户访问限制。
- [x] 测试 fixture 明确标注示例、无真实请求，不改 Vite 入口。浏览器验证 1440/760/375 几何、截图、375 关闭按钮 elementFromPoint 命中与真实点击、焦点返回、未接线零业务请求。fixture 不进入构建产物。
- [x] `npm run verify`、production-copy、safe-r1-build/browser 和结构/边界检查；保留共享基线失败，不改共享门禁绕过。
- [x] 文档给出真实 props/状态/样式接入、预览、证据及共享接入缺口；广泛独立审阅，修复问题并按项通知。更新现有 PR #2062 的真实范围和验证状态；不合并。

## Progress

- 开工：集成基线仍为 e703c6690，HEAD 22ebf6c71；现有未跟踪用户 prompt 保留。Task 1 开始。
- Task 1 complete：`3124a7823`，25 项测试与 typecheck 通过，独立审阅无发现。Squire 飞书分项通知 `om_x100b61145cfd30acce5a3e501b7f853`，适配器日志 `code=0`。
- Task 3 complete：`1f7c3529b`，34 项测试与 typecheck 通过，独立审阅无发现。Squire 飞书分项通知 `om_x100b6114665144a4cf6a2891f687d4b`，适配器日志 `code=0`。
- Task 2 complete：`7ec456f13` 加修复 `1acf7efff`、`edb44b714`、`e44e23ca8`；窄屏遮挡、延迟历史滚动恢复、加载中用户主动滚动均已修复。49 项聊天测试通过，独立复审无剩余发现；Squire 分项通知 om_x100b6114289d30b4b329648bb7069fd 已核对适配器 code=0。
- 开发服务已验证：`http://[::1]:4174/tests/dev-03/preview/index.html`；真实工作树 Chromium 打开，无 pageerror。此前 IPv4 预览由本次任务停止，当前只保留 IPv6 预览及其占位后端。
- 以上 focused tests 使用按原 lock 安装的 ext4 副本；工作树 `/mnt/d` 9p 上 Vitest worker 超时。每次记录源码/配置/依赖锁逐字节一致；未改共享测试或配置。

- Task 4 验证已执行：完整 GUI 229/229，私有浏览器 3/3，生产构建/文案和 safe-r1-build 通过；共享两个浏览器门禁仍因固定 macOS 路径失败。结构/模块边界检查通过。完整证据见 [e44e23ca8 验证记录](../notes/dev-03-frontend-2026-10-28/e44e23ca8/verification.md)。该验证点之后已完成完整分支复审和 PR 更新，最后结果见下文。

- 最终代码 `fc7c4678f`：完整分支复审通过；修复连接能力变化遮盖历史操作状态及未确认授权被误判未连接的问题。254/254 单元测试、3/3 私有浏览器、production-copy 和 safe-r1-build 通过。共享浏览器门禁仍 FAIL。最后结果见 [最终验证记录](../notes/dev-03-frontend-2026-10-28/fc7c4678f/verification.md)。已通过 Tailscale 推送，并更新草稿 PR #2062（目标 intg/gui-init、未合并）。最终 Squire 通知 om_x100b6114e0cc60a0cea77ba79e9ed7a 已确认适配器 code=0；四项通知均已实际投递至飞书。
