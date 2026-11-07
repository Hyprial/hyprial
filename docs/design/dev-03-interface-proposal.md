# dev-03 能力矩阵与接口提案

日期：2026-10-28。基线：`e703c6690b3bbcd16f558cb7293503d3c4208bb2`，分支 `feat/gui-dev-03`，目标 `intg/gui-init`。

本提案交由 work-developer 确认。下面的类型、目录和字段均为候选视图接口，不是已经发布的网络契约或冻结归属。用户随后授权按当前边界开始前端开发：新增私有受控组件和隔离测试，正式接入仍需集成人处理。实际组件接口以 [前端实施计划](dev-03-frontend-plan.md) 及交付源码为准；本提案不把待定后端接口变成必做需求。没有已验证的正式链路时，业务请求数必须为 0。

## 能力矩阵

| 页面动作 | 现有请求、返回与状态 | 代码证据（相对 gui/） | 当前结论与依赖 owner | 验证方式 |
| --- | --- | --- | --- | --- |
| 选择聊天对象 | `state.targets` → `select-target`；每个 target 一个线程 | `src/pages/ChatPage.tsx:104`；`src/state/appReducer.ts:85` | 展示基础存在；真实 Agent 列表由 dev-02 提供，共享投影由 work-developer 接入 | 空列表、失效选择、切换对象；不虚构多会话 ID |
| 无 Agent | `getActiveTarget` 回退首个对象，页面立即读取 `active.id` | `src/state/selectors.ts:35`；`src/pages/ChatPage.tsx:32` | 空列表可能崩溃；共享选择器可空契约由 work-developer 调整 | `targets: []` 不崩溃、无请求、有空态 |
| 会话草稿 | `drafts[targetId]`；当前 `set-draft` 隐式使用活跃对象 | `src/state/selectors.ts:42`；`src/state/appReducer.ts:117` | 共享草稿基础存在；跨对象异步事件需显式关联 target | A/B 输入不同文本，往返后逐字一致；迟到结果不覆盖新草稿 |
| 滚动位置 | 页面 ref 保存位置和是否贴底 | `src/pages/ChatPage.tsx:30`、`:38`、`:63` | 当前组件存活期间有实现；卸载后恢复未证实 | 两个长线程来回切换；非底部不被新消息强制拉到底部 |
| 发送可用性 | 仅检查 Agent availability；仍调用 `chat.send` 占位请求 | `src/state/selectors.ts:119`；`src/pages/ChatPage.tsx:77`；`src/adapters/stub.ts:100` | 不能证明业务链路 ready；work-developer 提供独立能力投影 | Agent 可用但后端未确认时，点击、Enter、重复触发均零业务请求 |
| 等待、失败、草稿 | 先 `send-message` 加入 `local-* / not-sent` 并清空草稿，再调用 stub；stub 返回类型只有 `ok:false` | `src/state/appReducer.ts:189`、`:209`；`src/adapters/useStubAction.ts:26` | 失败草稿不满足要求；共享 reducer/controller 由 work-developer 修改 | 延迟请求防重复；失败/未连接/未知保留草稿；后来编辑不被覆盖 |
| 未知结果、对账 | 已有独立 `OperationStatus` helper 和原请求 ID 关联规则；页面未接入 | `src/state/operation-lifecycle.ts:130`、`:220` | 可复用基础存在，真实控制器与对账读取未确认 | 超时不当确定失败；只查询原操作，不进行未知结果重发 |
| 历史、消息回执 | 按权威 ID 合并快照/事件、替换 local 消息的纯函数 | `src/state/messages.ts:11`、`:35`、`:53` | 纯函数不是正式 transport/state 回写；由 work-developer 接线 | 快照与事件去重、权威 ID 替换一次；受理不等于送达/回复 |
| 飞书四步 | ConnectionsPage 只有通用 adapter add/manage/authorize 占位动作 | `src/pages/ConnectionsPage.tsx:123`、`:137`、`:171` | 四步与独立结果缺失；同页含 dev-01 模型区域，需集成人拆分 | 选择 Agent → 授权 → 切换渠道 → 结果，各步独立等待/失败/不可用 |
| 未授权零请求 | 去授权按钮仅用 stub busy 禁用；共用测试仍要求占位调用 | `src/pages/ConnectionsPage.tsx:163`；`tests/connections.test.tsx:36` | 与本任务零请求要求冲突；共享接线和测试由 work-developer 一起调整 | 未接通时无 POST、授权跳转或弹窗；显示未连接/暂不可用 |
| 渠道状态供 dev-02 消费 | ConnectionSummary 仅 connected/unconfigured/disabled，无每 Agent 授权关系 | `src/state/gui-view-model.ts:61`；`src/components/Badge.tsx:31` | 缺少统一状态投影；dev-03 提案，work-developer 发布，dev-02 消费 | 管理页与渠道页显示同一权威状态；过期、撤销、未知不显示成功 |
| 键盘、中文输入法 | 关闭后有焦点返回；输入无 composition guard | `src/pages/ChatPage.tsx:90`、`:213` | 焦点实现存在，输入法安全未证实；私有边界确认后由 dev-03 处理 | 中文组字 Enter 不发送；键盘操作与焦点返回 |
| 1440/760/375 布局 | 全局 CSS 有桌面三列和窄屏重排；既有浏览器 probe 不等于中心真实点击 | `src/styles/app.css:1243`、`:2368`；`tests/dev-overlay-interaction.mjs:88`、`:184` | 实际几何未验证；全局修复归 work-developer | 根无横向溢出；375 关闭按钮中心命中自身/子元素，真实点击后关闭并返回焦点 |
| 复用组件 | 已有 Button、Field、Stepper、加载/空/错误/不可用组件 | `src/components/primitives/index.ts:1` | 可复用，无需新增包；公共导出和全局样式归集成人 | 受控 props、状态语义和样式接入说明；不复制业务真值 |

Python `AppChatAdapter` / `daemon_request_callable` 的存在，只说明有可复用底层代码。demo request 就绪必须提供林懿伦/姚升悦确认的“页面请求 → 可识别返回 → 正确回写”三项证据；或提供黄佳佳替代标准的版本、范围和集成人统一接线结果。

## 新取得的上游交接证据

用户提供的三段公告已与仓库固定提交核对：[GUI 联调公告原文](http://git.internal.example.com:3000/HyprialOS/harness-bridge/src/commit/a209031c6ba9171558e4126c17c2d8dac5d85ba5/docs/evidence/cli-command-surface/gui-team-daemon-request-announcement-2026-10-28.md)。Git blob 为 `80f382e36bf455d4a6e1313d03676c75bcc47bcd`，正文内容一致。这里引用的是固定版本交接事实，不是对上游 PR 当前状态的实时查询。

- 公告观察基线为 `intg/cli-command-surface@623e1f95c301074ad37a5a3e709872323b187e4e`，产品冻结版本 `069a10e41`；A05 公共客户端已整合，Python 消费方应通过 kernel 公共入口调用，不复制 IPC 或执行 CLI 充当 GUI 接口。
- 公告明确通用 GUI HTTP 网关未交付，四组旅程仍未全部接线；catalogue/OpenAPI 声明不能证明服务在线。不能将这些上游成果视为已在本工作树 `e703c6690` 中落地；该基线甚至尚无 `contract/daemon-request/` 目录。
- 完整迁入声明体系的只有 `link.add/drop/list/stats`；聊天和飞书需逐项核对旧 handler、契约和暴露方式。本组方法映射必须注明 params/result/errors、可信身份/权限、读写、超时/重试、确认/预览/幂等及验收步骤。
- 网关未 ready 时可继续方法映射和经归属确认的隔离测试；生产路径保持不可用，不发占位业务请求。共享 transport、身份和协议仍由公共接线 owner 收口。
- 2026-10-29 重新核对 `origin/dev@e228d4ae4` 的 `docs/design/gui-migration-next-phase-2026-10-28.md`：决策 `GUI-CONTRACT-CANCEL-20261028` 已取消 `intg/gui-refactor-contract`，撤回 PR #2043 的候选接口。此前指向该线的接入建议失效。统一请求入口与接口定义由后端负责人姚升悦确认；dev-03 不恢复候选协议，也不自行定义替代网关。PR 目标仍为用户指定的 `intg/gui-init`。

公告及其固定提交仍未包含三个冻结归属文档，不能据此解除共享文件限制。Squire 当前 inbox 和本机保留消息记录没有检索到这三条公告转述本身；这不影响已核验仓库原文，也不表示远端或飞书记录已删除。

具体方法、params/result/errors、身份权限、读写及重试/幂等映射见 [dev-03 daemon 方法表](dev-03-daemon-method-map.md)。关键待确认点是：`message.status` 按 messageId 查询，不能直接作为原 requestId 对账；`adapter.pin` 会改变 adapter 所有入站 DM 的 Agent 绑定，不能直接当成产品渠道切换；旧 router 没有飞书授权 RPC，旧 CLI 授权流程不能代替正式 GUI 网关。

## 共享接口候选

以下仅为讨论组件输入输出的 TypeScript 形状，不作为新的后端字段或私有全局状态实现。

```ts
type CapabilityView = {
  status: 'ready' | 'unavailable' | 'unconfirmed';
  reason?: string;
};

// 状态沿用现有 OperationStatus；不在 dev-03 再定义一份生命周期。
type SendOperationView = {
  targetId: string;
  requestId: string; // 正式控制器提供的原操作 ID
  status: OperationStatus;
  canRetry: boolean;
  canReconcile: boolean;
  error?: string; // 已清洗的用户文案
};

type LarkStep = 'chooseAgent' | 'auth' | 'switch' | 'result';

type MessageChannelView = {
  agentId: string;
  channelId?: string;
  connection: 'not-connected' | 'connected' | 'disabled' | 'unknown';
  authorization: 'unconfirmed' | 'not-authorized' | 'authorized' | 'expired' | 'revoked';
  reason?: string;
};
```

1. **能力与权限**：共享控制器按对象和操作提供发送、历史、授权、切换、对账能力；结合可信登录身份、dev-01 模型状态、dev-02 Agent 状态和已验证契约计算。缺失/未确认能力默认不可用，控制器和 UI 均阻止业务请求。请求途中能力变化不得擅自把已发请求标为未发送。
2. **聊天输入**：受控视图接收可空 activeTarget、targets、权威 messages、draft、capability、sendOperation 和历史读取状态；提交 `onSelectTarget(targetId)`、`onDraftChange(targetId,value)`、`onSend(targetId)`、`onRetry(originalRequestId)`、`onCheckOutcome(originalRequestId)`、信息面板折叠意图。视图不 fetch、不存另一份草稿或操作账本。
3. **草稿与发送**：控制器保存提交文本及修订号，提交时不清空。只有合同定义的明确受理条件满足且当前草稿仍是该修订时才清空。确定失败、不可用、未知均保留；后来的编辑不被旧结果覆盖。重复提交保护跨重渲染、会话切换、页面卸载有效。是否重试及幂等键复用规则由契约决定，未知仅查询原操作。
4. **读取、回执与错误**：提供 loading/empty/ready/error/stale 投影和正式消息回写事件；复用 messages.ts。读取失败保留最后权威数据并显示陈旧状态。区分权限/授权拒绝、请求前不可用、写入后传输不确定、读取失败和异常返回。明确成功究竟是受理还是送达，不从本地文本或点击推断成功。
5. **飞书流程**：受控输入包括 Agent 列表、选择、当前步骤、各步状态、授权能力/结果、切换操作及权威渠道结果；回调包括选择、返回/前进、授权、切换和查询原结果。返回保留选择；切换 Agent 后共享控制器使不再适用的旧授权/操作投影失效。授权成功不代表渠道切换成功，最终结果必须匹配 Agent/渠道。凭据、可信授权跳转和回调校验由共享运行时负责，不放浏览器存储或日志。
6. **状态共享**：上方 MessageChannelView 由集成人缩减/映射至正式契约并发布统一 selector。未授权、过期、撤销显示未连接；未知显示状态未确认；动作不可用显示暂不可用；已连接须有当前权威证据。旧数据读取失败必须显式标注陈旧。
7. **接入与测试**：App/AppShell、state 类型/reducer/selector、adapter/runtime、全局样式、共用测试与构建由 work-developer 修改。ConnectionsPage 由其提取渠道区域，保留 dev-01 模型区域。当前共用 connections 测试要求占位调用、state 测试要求提前清空草稿，需要随共享实现协调更新，dev-03 不私自改断言。
8. **浏览器门禁**：两个共享浏览器脚本写死 macOS Chrome；请求集成人支持显式浏览器路径或经验证的跨平台定位规则。不得跳过门禁或伪造平台路径；Linux 私有验收不能冒充原门禁通过。

## 候选私有文件边界

冻结包尚未取得，以下是请求确认的清单，不是已确定归属。

| 候选文件 | 目的 |
| --- | --- |
| `gui/src/pages/ChatPage.tsx` | 现有页面组合，仅在明确归属且共享控制器可用后接入 |
| `gui/src/pages/chat/ChatWorkspaceView.tsx` | 受控列表、消息与信息区，空态、滚动、焦点 |
| `gui/src/pages/chat/ChatComposer.tsx` | 受控草稿、IME、防重复触发、失败/未知动作呈现 |
| `gui/src/pages/channels/LarkChannelView.tsx` | 飞书四步与渠道状态展示，不写共享状态 |
| `gui/src/pages/chat/chat.css`、`gui/src/pages/channels/channels.css` | 必要时新增组件作用域样式；不覆盖全局根、外壳、开发层或既有全局选择器 |
| `gui/tests/dev-03/*.test.tsx` | 新私有行为测试；配置已有递归发现，无需修改 package.json |
| `gui/tests/dev-03/chat-browser.mjs` | 私有几何、真实点击、业务请求计数场景；调用入口须确认 |
| `docs/guides/dev-03-components.md` | 实现后提供真实 props、状态、回调和样式接入说明 |

共享入口、导出和共用测试的修改请求随上述组件交给集成人。先使用公共基线已有组件，不引入未集成的 shadcn 分支，也不为本任务建立私有路由或 adapter。

## 验收场景

以下为待执行测试规格，不表示已经通过。

1. 后端未连接、未确认，即使 Agent 可用也零业务请求、零重定向、零 local 成功；点击/Enter/重复触发均覆盖。
2. 无 Agent、失效选择、权威空历史均有可访问空态，不崩溃、不发请求、不编造对象。
3. A/B 不同草稿与长线程来回切换，草稿逐字保留，非底部滚动恢复；A 的迟到结果不影响 B。
4. 隔离控制器测试的延迟发送显示等待，阻止快速重复意图，保留草稿；明确失败、未知、明确受理分别断言。
5. 明确失败保留原文本，只有共享策略允许才重试；旧响应不能覆盖新编辑或其他会话。
6. 未知结果只查询原请求 ID；读取失败仍未知；对账不产生第二次写，重放回执不重复应用。
7. 快照/事件按权威 ID 去重，local 替换一次；重连失败保留陈旧历史，不伪造 Agent 回复。
8. 飞书选择 Agent 的加载、空、无效、不可用状态；键盘选择、返回保留；前进本身不触发授权成功。
9. 授权的等待、拒绝、失败、取消、过期、未知与确认状态独立；未授权显示未连接；无凭据存储/日志。
10. 切换渠道须满足授权及就绪条件；等待防重复，失败与未知区分；旧 Agent 的结果不让新选择成功。
11. 渠道页和 dev-02 消费同一状态；过期/撤销/未知/读取陈旧正确呈现。
12. 中文组字 Enter 不提交，结束组字后显式发送只触发一次；键盘、busy/disabled 和焦点返回正确。
13. 隔离浏览器 1440/760/375，记录高度、长文案和展开状态；根无横向溢出。1440 明确断言会话列表、消息区、输入区、信息区完整显示。375 测量关闭按钮中心，elementFromPoint 命中自身/后代，再执行无强制、无拦截真实点击，确认面板关闭、toggle 聚焦且 aria-expanded=false，保留截图与命中数据。
14. 在最终精确提交运行规定四条门禁及私有测试；独立审阅核查边界。共享失败如实报告，PR 只向 intg/gui-init，真实 Submitted-by、Skill-Impact、Contract-Impact，不自行合并。

## 待协调依赖

- work-developer 的准确 Hyprial URI；三个冻结文档的位置与版本：`module-boundaries-r3.md`、`product-handoff-r3.md`、`shared-file-ownership-r1.md`。
- dev-03 是否已有其他执行者，避免双写；上述私有清单、共页拆分与共享接线归属确认。
- demo request 三项证据或已 ready 的替代标准版本、接口与范围。
- 共享浏览器脚本的 Linux 执行方式。

依赖未确认期间完成只读分析、提案与基线报告，不自行扩大代码修改范围。分项通知和实际门禁结果见同目录 `dev-03-execution-2026-10-28.md`。
