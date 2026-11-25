# 群聊界面变更交接记录（第 1 片：看见群、读群）

文档版本：2026-11-21

配套规范：[GUI 设计与研发规范](../ui-development-standard.zh-CN.md) · [UI 变更交接模板](../ui-change-handoff-template.zh-CN.md)

## 1. 基本信息

| 字段 | 填写 |
| --- | --- |
| Change ID | `GC5-UI-01` |
| 状态 | `READY FOR REVIEW` |
| 产品 owner | 瑞华（未单独视觉验收） |
| 研发 owner | drafter（feat/gui-group-chat-read） |
| 平台 | `shared / web` |
| 页面/组件 | `gui/src/pages/ConnectedChatPage.tsx`、`gui/src/features/agent-chat/impl/{AgentChatSection,AgentTargetField,AgentHistoryField,target-presentation,chat.css}`、`gui/src/features/chat-channels/impl/{message-read-api,connected-chat,group/group-chat}.ts` |
| 目标分支 | `dev` |
| Applicable-Decision | `group-chat-ui-plan`（D3、D5、D6） |

## 2. 变更目标

### 2.1 本次目标（第 1 片：只读）

- 聊天页左栏顶部新增「群聊」分组，与现有 1:1 分组并列（D6）；每行展示群标题、头像与「N 位成员」，不含在线徽标与未读数。
- 支持点击群聊读取历史（`conversations.history`）、显示发送者名字（接通应答 `memberNames` 并按独立函数 `resolveGroupMemberName` 四级回退，含已离开成员）、回复引用、@ 提及与中文成员变动行（D5）。
- 支持每 2 秒自动增量刷新当前打开的群、加载更早消息（`older` 游标）、手动「刷新列表」与「刷新消息」。
- 群聊目录未知（`recordAvailable: false` 或目录读取失败）显式展示「群聊列表状态未确认。」而不伪装为空；`CONVERSATION_NOT_MEMBER` / `CONVERSATION_NOT_FOUND` / `DAEMON_UNAVAILABLE` 按状态矩阵表达。
- 本片群内不渲染输入框；旧版 `daemon` 无 `conversations.get` 能力时，`conversations.list` 不带 `kinds`，请求与改前逐字节一致。

### 2.2 非目标

- 群消息发送与回复（第 2 片）、建群与成员面板（第 3 片）、@ 选择器（第 4 片）。
- 后端/契约/Contract Mock 引擎/fixture 预览修改。

## 3. 单套 GUI 交付检查

- **预览与正式入口**：唯一聊天页面为 `gui/src/pages/ConnectedChatPage.tsx`，共用 `gui/src/features/agent-chat/impl/AgentChatSection.tsx`、`AgentTargetField.tsx`、`AgentHistoryField.tsx`。
- **数据源与能力差异**：`gui/src/app/capabilities.tsx` 在 `conversations.list` 与 `conversations.get` 均可用时注入 `groupChatRead`（`createGroupChatReadApiFromClient`）。正式入口走真实 daemon，Contract Mock 入口走同一生成客户端与 `/daemon/request/*` 传输。
- **生产隔离**：不修改 `gui/src/dev/`，不新增任何预览数据文件或方法名分派。

## 4. 状态与文案要点

- 群列表未知：`群聊列表状态未确认。`
- 成员变动：`{actor}创建了群聊「{title}」`、`{actor}添加了 {member}`、`{actor}移除了 {member}`、`{member} 退出了群聊`（`membershipEvent: true`，居中不折叠）。
- 成员拒绝/不存在：`你已不在这个群里。` / `这个群已不存在。`（提供「刷新列表」）。
- 文档暂不可读（`DAEMON_UNAVAILABLE`）：保留已读消息并提示「当前显示上次取得的消息。」。

## 5. 验证记录与未测范围

- 单元/组件/Contract Mock 测试与 `gate-gui.yml` 的 `gui-unit` 门禁：见本次工作汇报（一台远程 Linux 主机 实测结果）。
- **未测范围（NOT_RUN）**：
  - 真实浏览器 + 隔离真实 daemon 手工旅程（第 1 片浏览器第 1、2、3、8、9 步）。
  - 真实主体下 `from === identity` 显示为「你」（Contract Mock 主体为 `user:owner`、示例作者为 `user:alice`，属已知缺口 B4）。
  - 原生壳实机验证、读屏朗读、窄屏 390px 实机像素核对。

## 6. 剩余缺口

- `connected-chat.ts` 恢复既有代码换行后为 970 行（低于 1000 行硬上限），后续群逻辑继续留在 `impl/group/`，不再往 `connected-chat.ts` 叠加功能。
- 后端 B1（`memberNames`）已随 PR #3036 合入并接入 `message-read-api.ts` 与 `group-chat.ts`；Contract Mock 下用示例参数 `conversations.create`（`a.<id>` 成员）与 `conversations.members.remove`（含移除 `worker`）均可按新契约走通，第 2/3 片 `conversations.post` 已通过契约 `x-hyprial-mock-echo` 回显 `idempotencyKey → operationId`，尚余 B4 主体差异待处理。
- 聊天页入口 `initialize` 存量多请求问题仍由后续重构另行处理。

---

# 群聊界面变更交接记录（第 2 片：发消息、回复）

文档版本：2026-11-21

## 1. 基本信息

| 字段 | 填写 |
| --- | --- |
| Change ID | `GC5-UI-02` |
| 状态 | `READY FOR REVIEW` |
| 产品 owner | 瑞华（未单独视觉验收） |
| 研发 owner | drafter（feat/gui-group-chat-send） |
| 平台 | `shared / web` |
| 页面/组件 | `gui/src/pages/ConnectedChatPage.tsx`、`gui/src/app/capabilities.tsx`、`gui/src/features/chat-channels/index.ts`、`gui/src/features/agent-chat/impl/{ChatComposer,AgentChatSection,chat.css}`、`gui/src/features/chat-channels/impl/{daemon-api/group-post,group/group-send}.ts` |
| 目标分支 | `dev` |
| Applicable-Decision | `group-chat-ui-plan`（D1 选 A：内存键不占 1:1 键位，结果未知持续提示「发送结果未知；请在上方消息里确认是否已发出。」） |

## 2. 变更目标

### 2.1 本次目标（第 2 片：发消息、回复）

- 新增 `conversations.post` 适配器 `group-post.ts`：内存生成 43 字符 base64url 幂等键，summary 取正文首行前 80 字，校验应答 `operationId` 等于发出键及 `deliveries[0].accepted === true`，将 `CONVERSATION_NOT_MEMBER` / `CONVERSATION_NOT_FOUND` / `SUMMARY_REQUIRED` / `INVALID_ARGUMENT` 译为中文。
- 新增 `GroupSendController` / `useGroupSend`（`group-send.ts`）：持有群内草稿、回复目标、发送四态（发送中 / 已发送 / 失败 / 结果未知）及本地已发送条目；已发送消息立即展示并标「已发送」，下次刷新按 `messageId` 被记录条目替换且不重复。
- 扩展共享 `ChatComposer` 与 `AgentChatSection`：新增可选 `context` 插槽（展示「正在回复 <名字>：<摘要>」与取消回复按钮）、`unknownHint` 与 `onDismissUnknown`（「关闭提示」按钮，不自动重发、不提供重发按钮）。
- 普通群消息后通过 `renderAfterMessage` 插槽提供「回复」按钮；成员变动行与系统条目不渲染回复按钮；`CONVERSATION_NOT_MEMBER` / `CONVERSATION_NOT_FOUND` 历史拒绝态下隐藏输入框。
- 群发送结果未知时不写入 `sessionStorage`、不占用 1:1 恢复键位，1:1 消息发送保持正常。

### 2.2 非目标

- 建群与成员管理（第 3 片）、@ 选择器（第 4 片）。
- 不改后端、契约、Contract Mock 引擎或 `gui/src/dev/`。

## 3. 单套 GUI 交付检查

- **预览与正式入口**：继续共用 `ConnectedChatPage.tsx`、`AgentChatSection.tsx` 与 `ChatComposer.tsx`。
- **数据源与能力差异**：`capabilities.tsx` 通过具名工厂方法 `createGroupPostApiFromClient(client, { access: methodAccess })` 注入 `groupPost`（未新增内联箭头函数）。Contract Mock 已按契约 `x-hyprial-mock-echo`（`idempotencyKey → operationId`）把请求键回显到 `conversations.post` 成功应答中，前端发送在预览中可确认成功并清空草稿。
- **生产隔离**：未修改 `gui/src/dev/`，未新增方法名分派或跳过应答校验。

## 4. 验证记录与未测范围

- 单元/组件/Contract Mock 测试及 `gate-gui` 门禁实测结果：见本次工作汇报（一台远程 Linux 主机 执行）。
- **未测范围（NOT_RUN）**：
  - 真实浏览器 + 隔离真实 daemon 手工旅程（第 2 片第 3–6 步）。
  - 原生壳实机验证、读屏朗读、窄屏 390px 实机核对。

---

# 群聊界面变更交接记录（第 3 片：建群、成员）

文档版本：2026-11-21

## 1. 基本信息

| 字段 | 填写 |
| --- | --- |
| Change ID | `GC5-UI-03` |
| 状态 | `READY FOR REVIEW` |
| 产品 owner | 瑞华（未单独视觉验收） |
| 研发 owner | drafter（feat/gui-group-chat-admin） |
| 平台 | `shared / web` |
| 页面/组件 | `gui/src/pages/ConnectedChatPage.tsx`、`gui/src/app/capabilities.tsx`、`gui/src/features/chat-channels/index.ts`、`gui/src/features/chat-channels/impl/group/{group-admin.ts,group-admin.css,GroupCreateDialog.tsx,GroupMembersPanel.tsx}`、`gui/src/features/agent-chat/impl/{AgentTargetField.tsx,AgentChatSection.tsx,chat.css}` |
| 目标分支 | `dev` |
| Applicable-Decision | `group-chat-ui-plan`（§6 第 3 片、§8-D4） |

## 2. 变更目标

### 2.1 本次目标（第 3 片：建群与成员管理）

- 「群聊」分组标题右侧新增「新建群聊」按钮，打开 `GroupCreateDialog`（打开不发请求）；候选成员为聊天对象目录中带 `agentId` 的行与本机 agent 目录（`useAgentDirectory`）的交集，提交成员为 `a.<id>`。
- 依据 owner 裁定 D4：建群成功后不自动打开新群，新群插入「群聊」列表顶部并高亮，用户点击后才读取 `conversations.history`（建群动作仅发 1 个 `conversations.create` 请求）。
- 群标题栏新增「成员」按钮，点击后展开右侧信息栏并在其中渲染 `GroupMembersPanel`，仅在此时读取一次 `conversations.get`；展示成员 `displayName` 与 `kind`。
- `canManageMembers` 为真时允许加人（`conversations.members.add`）与移除人（`conversations.members.remove`）；owner/manager 行不渲染移除按钮；移除前通过共享 `Dialog` 确认一次；加减成员后直接用应答更新成员列表与成员数，不重读 `conversations.get`。
- `conversations.create`、`conversations.members.add`、`conversations.members.remove` 每次点击在内存中生成 43 字符 `idempotencyKey`；应答丢失、`DAEMON_UNAVAILABLE` 或应答校验不匹配时原位显示「结果未知。」与「重试」按钮，重试使用同一个键；明确拒绝展示对应错误原因。

### 2.2 非目标

- 群消息发送与回复（第 2 片）、@ 选择器（第 4 片）。
- 不改 `src/`、`contract/`、`gui/src/dev/`、`gui/src/client/generated/` 与任何基线/台账。

## 3. 单套 GUI 交付检查

- **预览与正式入口**：共用 `ConnectedChatPage.tsx`、`AgentChatSection.tsx`、`GroupCreateDialog.tsx` 与 `GroupMembersPanel.tsx`。
- **数据源与能力差异**：`gui/src/app/capabilities.tsx` 通过工厂 `createGroupAdminApiFromClient` 注入 `groupAdmin`（不在 `capabilities.tsx` 新增内联请求箭头）。Contract Mock 下 `conversations.get`、`conversations.members.add`、`conversations.members.remove` 与匹配示例参数的 `conversations.create` 可走通，非示例参数 `conversations.create` 如实返回 `MOCK_EXAMPLE_MISSING`。

## 4. 验证记录与未测范围

- 单元/组件/Contract Mock 测试与 `gate-gui.yml` 的 `gui-unit` 门禁：见 `report-gc3.md`（一台远程 Linux 主机 实测结果）。
- **未测范围（NOT_RUN）**：真实浏览器 + 隔离真实 daemon 手工旅程、原生壳实机、读屏朗读、窄屏 390px 实机像素核对。

---

# 群聊界面变更交接记录（第 4 片：@ 成员选择器）

文档版本：2026-11-21

## 1. 基本信息

| 字段 | 填写 |
| --- | --- |
| Change ID | `GC5-UI-04` |
| 状态 | `READY FOR REVIEW` |
| 产品 owner | 瑞华（未单独视觉验收） |
| 研发 owner | drafter（feat/gui-group-mention-picker） |
| 平台 | `shared / web` |
| 页面/组件 | `gui/src/pages/ConnectedChatPage.tsx`、`gui/src/features/chat-channels/index.ts`、`gui/src/features/chat-channels/impl/group/{GroupMentionPicker.tsx,group-admin.ts,group-admin.css,group-chat.ts,group-send.ts}`、`gui/src/features/agent-chat/impl/{ChatComposer.tsx,AgentChatSection.tsx}`、`gui/src/state/gui-view-model.ts` |
| 目标分支 | `dev` |
| Applicable-Decision | `group-chat-ui-plan`（§6 第 4 片，owner 2026-11-21 裁定「群消息只唤醒被 @ 的 agent」） |

## 2. 变更目标

### 2.1 本次目标（第 4 片：@ 成员选择器）

- 新增 `GroupMentionPicker`（`gui/src/features/chat-channels/impl/group/GroupMentionPicker.tsx`）：输入框操作区旁新增「@」按钮，点开列出当前群的 agent 成员（`kind === 'agent'` 且不含自己），支持多选；不做「打字输入 @ 弹出」的行内补全。
- 复用 `group-admin.ts` 既有的成员读取与缓存：新增 `ensureMembersLoaded`，在 `executeCreate` 建群成功时同时写入 `groups` 缓存。打开群后若未读过成员（未点过「成员」、也非刚建的群），点「@」时读一次 `conversations.get`（恰好 1 个请求）；已读过则不再发起请求。
- 已选成员在输入框上方的 `context` 行以芯片形式展示（与「正在回复 …」并存），支持逐个移除；发送时作为非空 `mentions`（成员 id 数组）随 `conversations.post` 发送，发送成功后清空。
- 回复一条 agent 消息时，通过 `ConversationMessage.authorId` 将被回复者预先加入已选 `@`（用户可见、可去掉）；成员变动行与自己的消息不预选任何人。
- 键盘可操作：Tab 到「@」按钮、Enter 打开选择器、Escape（或关闭按钮）关闭后焦点回到「@」按钮。

### 2.2 非目标

- 打字输入 `@` 触发的行内补全。
- 不改 `src/`、`contract/`、`gui/src/dev/`、`gui/src/client/generated/`、`gui/src/app/capabilities.tsx` 与任何白名单/基线/台账。

## 3. 单套 GUI 交付检查

- **预览与正式入口**：继续共用 `ConnectedChatPage.tsx`、`AgentChatSection.tsx`、`ChatComposer.tsx` 与 `GroupMentionPicker.tsx`。
- **数据源与能力差异**：未修改 `gui/src/app/capabilities.tsx`，继续通过既有 `groupAdmin`（`conversations.get`）与 `groupPost`（`conversations.post`）端口驱动；未注入 `groupAdmin` 时不渲染「@」按钮。
- **生产隔离**：未修改 `gui/src/dev/`，未新增方法名分派或内联请求箭头函数。

## 4. 验证记录与未测范围

- 单元/组件测试（`group-mention.test.tsx` 等）及 GUI 门禁实测结果：见 `report-pick.md`（一台远程 Linux 主机 执行）。
- **未测范围（NOT_RUN）**：真实浏览器 + 隔离真实 daemon 手工旅程、原生壳实机、读屏朗读、窄屏 390px 实机像素核对。
