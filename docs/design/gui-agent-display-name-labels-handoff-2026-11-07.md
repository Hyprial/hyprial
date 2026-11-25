# GUI agent 标签统一用目标 displayName：交接（cloud-claude → 下一 session，2026-11-07）

- 交接时间：2026-11-07。本 session 只做普查和交接，不改代码。
- 基线：`release/v0.5.5.4` @ `baf3f86b1`（UUID 寻址 #1886 + #2546 已合入）。下文 file:line 都以这个提交为准。
- 本分支：`feat/gui-agent-display-name-labels`，只含本文件；WIP PR → `release/v0.5.5.4`。
- 前置文档：`docs/design/uuid-addressing-handoff-2026-11-04.md`（§0 的 squire 裁定）、`contract/identity/agent-addressing.md`。
- 并行交接：「坐在电脑前操作 GUI 的那个人」改用登录 daemon 那个 user 的 user-proxy 来指代，另有一份 handoff 和 WIP PR。消息作者里属于**用户一侧**的标签（`我`、`你`、`user:` URI）归那份文档，本文只管 **agent 一侧**。

## 0. 裁定、范围和原因

- **Owner 裁定（Allen 2026-11-07）**：GUI 里凡是指称某个 agent 的标签，一律用**目标 agent 的 `displayName`**。
- **已有规则**（chat 目标已经在用）：`displayName`（非空）优先，否则显示**完整 agent URI**。不从 actor 或 URI 段推导名字，也不拿占位文字顶替。唯一实现是 `gui/src/features/agent-chat/api/target-display-name.ts:14` 的 `agentTargetDisplayName`，测试在 `gui/tests/features/agent-chat/target-display-name.test.ts`。
- **原因**：UUID 寻址之后，agent URI 是 id 形态（`agent:<owner>:<node>:a.<id>`），名字可以 rename。squire 降为普通 agent（2026-11-06 裁定），前端靠自己存的 `agentId` 认出它，不靠名字。所以写死的 `'Squire'`、拿 actor 当标签、拿 URI 当作者名，在 rename 之后都会显示错，或显示成没有意义的 id。
- **范围**：`gui/src/**` 和 `src/hyprial/shell/impl/gui/**`。Python 侧只透传标签字段，不生成标签：
  - `src/hyprial/shell/impl/gui/adapter/chat.py:176-180`、`:281-285`：`actor`、`agentId`、`displayName` 原样透传。
  - `src/hyprial/shell/impl/gui/live/personal.py:1-21`：按存下的 `agentId` 选出 `preferredTargetUri`；还没存 agentId 时走 legacy fallback（agent.list 里 actor 为 `squire` 的唯一本地行，`:12-19`）。两条路径都只选键，不生成标签。
  - 普查结论：Python 侧**无需改动**。
- **不在范围内**：
  - 用户一侧的标签（见并行 handoff）。
  - 后端 squire 链的删除（uuid handoff §0 后续 1）。
  - 标识符和类型名（`SquireStep`、`prepareSquire`、`SquireIdentityStore`、`'squire'` step id、错误码 `SQUIRE_*`）：它们不是 UI 标签，不改。

> **S2d 状态说明（2026-11-22）**：上述首启专用标识符（`SquireStep`、`prepareSquire`、`SquireIdentityStore`、`FirstRunStepId='squire'` 与 `squireState=name-frozen`）已于 S2d 一并删除；普通 Agent 的 `displayName` 显示规则与 chat/Agents 行为保持不变。

## 1. 交接 prompt（直接转发给接手的 session）

```
你接手 hyprial GUI 的「agent 标签统一用目标 displayName」，向 Allen 汇报。

0. 仓库与身份
- 仓库 https://code.hyprial.com/HyprialOS/harness-bridge ，先读 AGENTS.md。
- 基线：release/v0.5.5.4 @ baf3f86b1（UUID 寻址 #1886 + #2546 已合入）；交接文档里的 file:line 都以它为准。
- 交接文档：feat/gui-agent-display-name-labels 上的
  docs/design/gui-agent-display-name-labels-handoff-2026-11-07.md（WIP PR → release/v0.5.5.4）。
- CI 规定：带 GUI 路径（gui/**、src/hyprial/shell/impl/gui/** 等）的 PR 和非 dev 分支 push，
  head（或非 dev 的 base）必须是 intg/gui*，否则 CI_ROUTE 报错（scripts/ci/ci-plan.mjs:12、:210-217）。
  所以从 feat 分支切出 intg/gui-agent-display-name-labels 来实施，新开 PR → release/v0.5.5.4，
  合入后 close 那个只含文档的 WIP PR（或经 Allen 同意，换掉 WIP PR 的 head）。
- 提交身份：cloud-claude <cloud-claude@agents.hyprial.com>，末尾加 Co-Authored-By。
  不 force-push，不改写历史；同步 release 一律用 merge。

1. 先读
- 本交接文档 §0、§2、§3、§4。
- gui/src/features/agent-chat/api/target-display-name.ts（唯一的取名规则）。
- docs/design/uuid-addressing-handoff-2026-11-04.md §0（squire 是普通 agent，靠 agentId 认）。

2. 规则（不要再问）
- 标签 = displayName（非空）?? 完整 agent URI，一律走 agentTargetDisplayName。
- 标签和键分开：URI 或 agentId 作键，标签只用来显示，不能拿标签去比较或回传。
- 用户一侧的标签不归你管（并行 handoff：user-proxy 指代）。

3. 待办（按 §5 的顺序，每一步先写失败的测试）
a. AgentDirectoryRow 补上 uri，displayName 的兜底从 actor 改成 uri（§4 A6）。
b. 非 onboarding 的违规点 A7–A11（provider 选择器、发送会话选择、只读历史作者、link 对端）。
c. onboarding A1–A5：写死的 'Squire' 改成 conversation.targetName。没有目标时显示什么，按 Q1 的裁定。
d. 产品文案 B 类：按 Q2 的裁定处理；裁定之前不动。
e. dev-only C 类：不必改，最多顺手改成走 displayName 的数据。

4. 门禁
- cd gui && npx vitest run <§6 列出的文件>，再跑 npm run typecheck、npm test。
- 浏览器验收脚本（*.mjs）里写死的 '与 Squire 的消息' 要一起改（§6）。
- 结束时报告：每个违规点改了哪里、对应哪个测试；Q1、Q2 采用了哪个裁定。
```

## 2. 已定的决定

- **D1 取名只有一个入口**：所有 agent 标签都调用 `agentTargetDisplayName`。
  - agent.list 行要传 `{ targetUri: row.uri, displayName: row.displayName }`。注意 agent.list 的 `actor` 是 registry 短名（见 `live/personal.py:15-16`），不能当 `targetUri` 传。
  - 不新写第二个兜底函数，`nonEmptyStringOr(row.displayName, actor)` 这类写法要换掉。
- **D2 键和标签分开**：URI 或 agentId 作 React key、比较条件、回传参数，标签只用于显示。
  - 现有的反例：link 的 `peerLabel` 同时被当成键用（§4 A11），必须先拆出 `peerUri`，再改标签。
- **D3 squire 没有特殊标签**：GUI 按存下的 `agentId` 认出用户的 squire（`app/capabilities.tsx:94`、`model/model-key-save-adapter.ts:20-23`、`live/personal.py`）。显示时和其他 agent 一样用 displayName，rename 之后显示新名字。
- **D4 次要信息里的 URI 可以保留**：chat 目标的副标题 `role: id`（`agent-chat/api/chat-controller.ts:68`），以及 Lark 区块 `title={binding.actor}` 的 tooltip（`LarkBindingSection.tsx:92`），这两处是在主标签之外给出完整 URI，不算违规。
- **D5 Python 侧不改**（§0）。

## 3. 待裁问题（需要 Allen 裁定）

- **Q1 还没有目标时显示什么**。适用于：首跑聊天服务未接通、没有可用目标时的禁用 composer（`FirstMessageComposer.tsx:35`），以及 `useFirstRunConversation.ts:84` 的兜底。这时既没有 displayName，也没有 URI。
  - (a) 中性词。仓库已有先例：`chat-channels/SingleAgentChatSend.tsx:150` 用 `'未选择对象'`，aria-label 会变成「给 未选择对象 的消息」。也可以用「聊天对象」。
  - (b) 保留 `'Squire'`，当作产品里的角色词。
  - (c) 没有目标就不渲染 composer，只显示已有的 Notice（`ConversationStep.tsx:63-70`、`:138-142`）。
  - **建议**：选 (a) 并统一成 `'未选择对象'`。(c) 会改变「禁用但可见」的既定设计（`FirstMessageComposer.tsx:8-13` 的注释）。
- **Q2 onboarding 文案是否保留 `Squire` 这个产品词**。B 类文案描述的是「将要创建的那个个人助理」，创建之前没有 displayName 可用。
  - **建议的分法**：
    - 讲**创建、启动这个角色**的文案（步骤名、`SquireStep`、准备失败提示、Settings 里的「继续准备」）保留 `Squire`，当作产品词。
    - 讲**已经创建好的那个 agent** 的文案（`ConversationStep.tsx:35`、`:57`、`:140`、`ModelCredentialSettings.tsx:68`、两份 chat-controller 里的 LiveError）改成 displayName，或改成不指名的说法。
  - 请 Allen 确认这个分法，或者裁定全部改成中性词（例如「个人助理」或「Agent」）。
- **Q3（小）**：Agent 列表和详情上的「预配置」徽标，靠 fixture id `'agent-squire'` 判断（`agents/agent-extensions.ts:110`、`:116`）。live 模式下它恒为 false（`agents/api/index.ts:723`）。是保留为 dev 专用，还是删掉？建议保留，不在本线处理。

## 4. 普查清单（基线 `baf3f86b1`）

分类说明：
- **A** 必须改：标签不是来自 displayName。
- **B** 产品文案：把 `Squire` 当作产品词，按 Q2 处理。
- **C** dev-only 的 fixture 或预览：可以不改。
- **OK** 已经合规，作参照。

### A：必须改

| # | 位置 | 现状 | 改法 |
|---|---|---|---|
| A1 | `gui/src/features/onboarding/useFirstRunConversation.ts:84` | `targetName: target?.displayName ?? 'Squire'` | `target` 存在时它的 displayName 已经由 `agentTargetDisplayName` 算好（`chat-controller.ts:67`），兜底只剩「没有目标」这一种情况，按 Q1 处理。 |
| A2 | `gui/src/features/onboarding/FirstMessageComposer.tsx:35` | 禁用的 composer 上写死 `targetName="Squire"`，aria-label 变成「给 Squire 的消息」（`agent-chat/ChatComposer.tsx:68`） | 改成 Q1 裁定的词。 |
| A3 | `gui/src/features/onboarding/ConversationStep.tsx:120-124`（`:122`），在 `:164` 使用 | `AUTHOR_LABELS.assistant = 'Squire'`，每条对端消息的作者都显示成 Squire | 改成 `conversation.targetName`，参照 `agent-chat/AgentHistoryField.tsx:34`。`user` 和 `system` 两项不动（用户一侧见并行 handoff）。 |
| A4 | `ConversationStep.tsx:148` | `aria-label="与 Squire 的消息"` | 改成 `` `与 ${conversation.targetName} 的消息` ``，参照 `AgentHistoryField.tsx:29`。 |
| A5 | `ConversationStep.tsx:35`、`:57`、`:140` | 这几处文案指的是已创建的 agent（「向你的 Squire 发送…」「向 Squire 发送…」「未提供可用的 Squire 对象」） | 按 Q2：`:35` 只在有 `conversation` 时渲染（`:33`），用 targetName；`:57` 所在分支通常没有 runtime（`conversation` 缺失或 state 为 completed），改成不指名的说法；`:140` 是没有目标的情况，也改成不指名。`:51` 是锁定态（创建之前），归 B 类。 |
| A6 | `gui/src/features/agents/api/index.ts:326` | `displayName: nonEmptyStringOr(row.displayName, actor)`：兜底是 actor 短名，不是完整 URI。`AgentDirectoryRow`（`:83-101`）没有 `uri` 字段，而 agent.list 行本身有 `uri`（`client/generated/types.ts:139`）。文件头注释 `:37-39` 记的也是「缺省回退 actor」，要一起改 | `AgentDirectoryRow` 加上 `uri`，改成 `agentTargetDisplayName({ targetUri: row.uri, displayName: row.displayName })`。它会影响 Agent 列表和详情（`AgentList.tsx:312-382`、`AgentDetail.tsx:244-250`）、`ContactsPage.tsx:303`、`:378`，以及只读会话列表（`MessageSessionsSection.tsx:193-224`）。 |
| A7 | `gui/src/features/agent-provider/api/index.ts:68` | `{ actor: row.actor, label: row.actor }`，在 `AgentProviderSection.tsx:23` 的 Agent 选择器里渲染 | `label` 改成 `agentTargetDisplayName({ targetUri: row.uri, displayName: row.displayName })`。`actor` 仍然作为选项值，因为 provider API 收的是 actor。 |
| A8 | `gui/src/features/chat-channels/ConnectedChatSendPicker.tsx:65`、`:71` | 下拉选项是 `{row.targetUri} · {row.conversationId}`，传给 `SingleAgentChatSend` 的是 `displayName: choice.targetUri` | `SendDirectoryTarget`（`send-session-selection.ts:4-8`）补上 `displayName`（targets.list 行本身带这个字段，`types.ts:1261`；`connected-chat.ts:67-73` 的 `ConnectedChatTargetRow` 已经有）。下拉渲染的是 `SendSession`（`send-session-selection.ts:10-13`），它也要带上名字：`sendSessionsFromMessages`（`:34`）从目录行带过来。用 `agentTargetDisplayName` 取名，选项显示「名字 · 会话 id」。注意：基线上 production 没有接这个 picker（`AppShell.tsx:88` 渲染的 `<ConnectedChatPage />` 不传 `chatSendDirectory`，见 `ConnectedChatPage.tsx:70-76`），目前只在测试里出现，所以这是潜在问题，照样修。 |
| A9 | `gui/src/features/chat-channels/MessageSessionsSection.tsx:171` | 只读历史的作者直接显示 `{entry.from}`（URI） | 如果 `from` 是 agent，就在目录行里按 uri 查到它的 displayName，查不到就显示完整 URI。如果 `from` 是用户，按并行 handoff 处理。 |
| A10 | `gui/src/features/agents/detail/OverviewTab.tsx:197`、`agents/detail/agent-detail-format.ts:128` | link 行和 link 通知直接显示 `row.peer`（URI） | 在 agent 目录里按 uri 查 displayName，查不到就显示完整 URI。key（`OverviewTab.tsx:196`、`agent-detail-format.ts:125`）继续用 peer。 |
| A11 | `gui/src/features/org/api/link-request-api.ts:85` | `peerLabel: link.peer`，标签就是 URI | **先拆键（D2）**：`peerLabel` 目前也被当成键或回传值用，包括 `crosstab/AgentOrgPanel.tsx:204`（add-link 预填 `peer`，经 `:270` 回传给 `gateway.addLink`）、`sections/topology-model.ts:62`、`:82`（`peerNodeId(peerLabel)` 作拓扑节点 id）、`dev/fixture-gateway.ts:723`、`:733`（去重和新建 link）。先在 `org/domain/types.ts:258`、`:267`、`:287` 加上 `peerUri`，键改用 `peerUri`，然后 `peerLabel` 改成 displayName ?? URI。live 的 `AgentLink.id` 已经是 peer URI（`link-request-api.ts:83`），fixture 的 id 不是（`lk-new-*`），所以仍要显式的 `peerUri`。纯显示的地方：`AgentOrgPanel.tsx:154`、`:199`、`:278-279`，`TopologyViews.tsx:177`、`:186`、`:395`（及 `:397`、`:404-405`）、`:465`、`:482`（及 `:484`、`:492`），`ChatRefusalHint.tsx:34`、`:64`、`:79`。另外，`RefusedMessage` 和 `ChatRefusal` 在 live 下没有来源（`domain/unwired.ts:20-21` 恒为 null 和 `{}`），目前只有 fixture 数据；以后接线时直接带 `peerUri`。 |

**相关疑点（不是标签问题，顺手核实）**：`MessageSessionsSection.tsx:125-127` 用 `conversation.peer`（消息里的完整 URI，见 `:31-34`）去和 `row.actor`（agent.list 短名）比较。在真实数据里两者对不上，所以 Agent 和会话的关联可能永远为空。测试 `gui/tests/chat-channels/message-sessions-section.test.tsx` 的 fixture（`:14` 的 peer，`:19-27` 把 peers 直接当 actor 传进 `agent()`）把 actor 写成了完整 URI（还是名字形态的 `agent:owner:node:worker`），正好掩盖了这个问题。A6 加上 `uri` 之后，这里应改为按 `row.uri` 匹配；先写一个失败的测试确认。

### B：产品文案（`Squire` 作产品词，按 Q2 处理）

- **onboarding 步骤和创建流程**：
  - `onboarding/FirstRunOnboarding.tsx:19`（步骤名 `squire: 'Squire'`）
  - `onboarding/SquireStep.tsx:32`、`:42`、`:44`、`:47`、`:59`、`:60`
  - `onboarding/ModelStep.tsx:113`
  - `onboarding/ConversationStep.tsx:51`
  - `onboarding/presentation.ts:158`、`:198`、`:231`
  - `onboarding/actions.ts:16`、`:18`、`:27`、`:79`、`:85`
  - `onboarding/auth-actions.ts:173`、`:174`、`:177`、`:179`、`:181`
  - `onboarding/useFirstRunController.ts:390`、`:468`、`:476`
  - `onboarding/types.ts:127`（`SQUIRE_NAME_FROZEN_MESSAGE`，经 `auth-actions.ts:180` 显示）
  - `onboarding/preview.tsx:56`
- **设置页和开始页**：
  - `pages/SettingsPage.tsx:49`
  - `pages/StartPage.tsx:25`
  - `features/user-settings/user-proxy/UserProxySection.tsx:33`、`:39`
  - `features/model/ModelCredentialSettings.tsx:68`（「当前首次使用由 Pi 运行 Squire。」）：指的是已创建的 agent。Q2 的建议是改成不指名，或改成 displayName。
- **错误提示**：`LiveError('invalid-response', '个人 Squire 地址无法确认。')`，在 `gui/src/features/agent-chat/api/chat-controller.ts:87` 和 `gui/src/adapters/live/chat-controller.ts:87` 各有一份，两份内容相同，要一起改。这句说的是 `preferredTargetUri` 不在目标列表里。Q2 的建议是改成「首选聊天对象地址无法确认」。

### C：dev-only（可以不改）

- `dev-mode/session/session-reducer.ts:20`、`:24`、`:25`（`SQUIRE_NAME`、`FIRST_MESSAGE`、`SQUIRE_REPLY`，在 `:217`、`:244`、`:251` 使用）。这些是数据，名字作为 `name` 字段流入。
- `dev-mode/DevScenarioSelector.tsx:14`（场景名「Squire 失败」）、`dev-mode/first-run-capabilities.ts:57`、`:60`（模拟消息）。
- `onboarding/FirstRunOnboarding.tsx:161-162`：只在 `import.meta.env.DEV && scenario === 'demo'` 时出现的隔离预览 Notice（`:155`）。
- `org/sections/StatsSection.tsx:156`（`pair.from → pair.to`）：`OrgStats` 在 live 下没有来源，只有 fixture 的名字数据；以后接线时按 D1 处理。
- `fixtures/demo.ts:18-20`（`displayName: 'Squire'` 是 fixture 数据，经过 displayName 显示，合规）。
- `features/org/dev/fixture-data.ts:137`、`:175`、`:234`、`:235`、`:239`、`:391`，以及 `features/org/dev/scenarios.ts:69`。
- 固定 id：`agents/agent-extensions.ts:16`（`SQUIRE_AGENT_ID`，「预配置」标记，见 Q3）、`quick-chat/quick-chat-constants.ts:2`、`state/appReducer.ts:18`、`App.tsx:250`。这些是非 live 模式的 fixture 键，不是标签。

### OK：已经合规（参照写法）

- **chat 目标**：`agent-chat/api/chat-controller.ts:67`、`:78`（`adapters/live/chat-controller.ts` 里同样的两行）。
- **chat-channels**：`chat-channels/connected-chat.ts:138`，`chat-channels/daemon-api/lark-binding.ts:148`。
- **Lark 页面**：`pages/live/LiveLarkPage.tsx:39-40`，`connections/lark-binding/LarkBindingSection.tsx:40`（兜底是完整的 pin URI）。
- **显示 `target.displayName` 的组件**：
  - `agent-chat/AgentHistoryField.tsx:29`、`:34`
  - `agent-chat/ChatComposer.tsx:68`
  - `agent-chat/AgentChatSection.tsx:109-136`
  - `quick-chat/QuickChat.tsx:113-175`
  - `chat/ChatMessages.tsx:55`
- **测试**：`gui/tests/chat.test.tsx`、`modules/dev03/j3-chat.test.tsx`（含 `:332` 的「快捷对话与 Squire 的消息」）、`modules/dev03/j4-feishu.test.tsx`、`support/shared-views-browser.mjs:34`、`:55` 里的「给/与 Squire 的消息」来自 fixture 的 `displayName: 'Squire'`，合规，不用改。

## 5. 建议的实施顺序（每一步对应一个提交）

1. A6：`AgentDirectoryRow.uri` 加上 displayName 兜底改为 URI。这是 A9、A10、A11 查名的基础。顺手核实并修复 §4 的相关疑点。
2. A7、A8：provider 选择器和发送会话选择器，都是改数据层的标签。
3. A11：先拆 `peerUri`（只改键，测试应保持全绿），再改 `peerLabel`。A10 和 A9 共用同一个按 uri 查名的 helper。这个 helper 放在 `features/agents/api` 或 `agent-chat/api` 都可以，但底下只能调用 `agentTargetDisplayName`。
4. A1–A5：onboarding。没有目标时按 Q1。
5. B 类：Allen 裁定 Q2 之后再做。没有裁定就不动，在 PR 里写明。

## 6. 测试计划（vitest，`cd gui && npx vitest run <file>`）

- **要更新的现有断言**：
  - `gui/tests/features/agents/agents-api.test.ts:99-111`：`['writer', 'writer']`、`['archivist', 'archivist']` 改为期望完整 URI。fixture 行要带上 `uri`。
  - 禁用 composer 的 aria-label（「给 Squire 的消息」）改为 Q1 裁定的词：`gui/tests/first-message-composer-b-fix-009.test.tsx:46`、`gui/tests/dev01-conversation-preview.test.tsx:165`。同文件 `:29`（直接给 `ChatComposer` 传的 prop）和 `:62`（ready runtime 的 fixture 输入）不是断言，不用改。
  - `gui/tests/dev01-conversation-preview.test.tsx:62`、`:121`：log 的 aria-label 从「与 Squire 的消息」改为「与 Fixture Squire 的消息」，和 `:58` 的 composer 名对齐，因为两者都来自 targetName。
  - `gui/tests/chat-channels/message-sessions-section.test.tsx:19-27`：fixture 的 actor 改用短名，`uri` 用完整 URI（对应 §4 的疑点）。
  - A7 没有现成断言可改：`gui/tests/client/consumer.test.ts:106-128` 测的是测试自带的 helper `gui/tests/client/agent-provider-feature.ts`（label 取 `name`），不是 production 的 `listAgents`；`gui/tests/agent-provider.test.tsx:37`、`:42`、`:148` 是直接传入 label 的 mock。所以 A7 要新增用例（见下）。
  - 浏览器脚本（不在 vitest 里，但要同步改）：`gui/tests/auth-browser.mjs:97`、`gui/tests/dev01-onboarding-browser.mjs:454`。
- **要新增的用例**：
  - 首跑对话中，目标 displayName 为 `'Alfred'`（squire 被 rename 过）时，作者标签、log 的 aria-label、composer 的 aria-label 都显示 `Alfred`，DOM 里不出现 `Squire`。放在 `dev01-conversation-preview.test.tsx` 或 `live/first-run-live-composition.test.tsx`。
  - 目标没有 displayName 时，上面三处都显示完整 id 形态 URI（`agent:o:n:a.<id>`）。
  - `unified-backend/settings-client.test.ts`（或新文件）：`createAgentProviderApiFromClient(...).listAgents()` 的 label 是 displayName；displayName 为空或缺失时是行的 `uri`，不是 actor 短名。
  - `chat-channels/connected-chat-send-picker.test.tsx`：选项和 `SingleAgentChatSend` 用 displayName；没有 displayName 时用 URI。
  - `chat-channels/message-sessions-section.test.tsx`、`connected-chat-history.test.tsx`：只读历史里 agent 作者显示 displayName。
  - `modules/dev04/org-link-principal.test.tsx`（以及 `support/org-backend.ts`）：`peerLabel` 是 displayName，`peerUri` 是 URI；add-link 预填、拓扑节点 id 用 `peerUri`。
  - `ui/jump-action-compact.test.tsx` 或 `features/agents/agent-detail-tabs.test.tsx`：link 行和 link 通知显示 displayName。
- **可选的守卫**：加一个 vitest 用例，扫描 `gui/src/features/**/*.tsx`，要求 JSX 属性 `targetName=`、`aria-label=` 里不出现 `Squire` 字面量（`dev/` 和 `dev-mode/` 除外）。
- **收尾**：`npm run typecheck`、`npm test`（`gui/package.json:19`）。

## 7. 验证计划

- **单元**：§6 全绿；`git grep -nw Squire -- gui/src ':!gui/src/dev-mode' ':!gui/src/features/org/dev' ':!gui/src/fixtures'` 只剩注释和 Q2 允许保留的 B 类文案。用 `-w` 而不是只搜 `'Squire'`：A2 的 `targetName="Squire"`、A4 的 aria-label 和 A5 的正文都不是单引号字面量；`-w` 也不会匹配 `SquireStep` 这类标识符。
- **dev 预览**（不需要 daemon）：在 dev-mode 的首跑场景里，把 fixture 的 displayName 改成别的名字，看对话步骤中作者、log 和 composer 是否跟着变。
- **真实环境**（属于 release 上的动作，不是 merge 前提）：
  1. `hyprial agent rename` 一个 squire。
  2. 刷新 GUI，确认首跑对话、Agent 列表和详情、provider 选择器、发送会话选择、只读历史、组织页 link 都显示新名字。
  3. 把 displayName 清空，确认这些地方都显示完整 id 形态 URI。

## 8. 坑点

- **agent.list 的 `actor` 不是 URI**：它是 registry 短名（`live/personal.py:15-16`），不能当成 `agentTargetDisplayName` 的 `targetUri` 传，否则兜底显示的会是短名，正好违反规则。
- **chat-controller 有两份**：`features/agent-chat/api/chat-controller.ts` 和 `adapters/live/chat-controller.ts`，除 import 外内容相同，改一份就要同步另一份。
- **`peerLabel` 被当成键用**（A11）：直接把它改成 displayName，会破坏 add-link 预填和拓扑节点 id，重名的 agent 还会合并成同一个节点。
- **CI 的 GUI lane**：GUI 改动必须走 `intg/gui*` 分支（`scripts/ci/ci-plan.mjs:12` 定义，`:210-217` 强制；PR 和非 dev 分支的 push 都检查）。只改文档的 feat 分支不受影响：`docs/**/*.md` 由 `isDocumentation` 判为 prose（`scripts/ci/ci-scope.mjs:13`、`ci-plan.mjs:36`、`:45`），不算 GUI 组件。


## 9. 实施记录（2026-11-08）

- 实施分支：`intg/gui-agent-display-name-labels`，目标为 `release/v0.5.5.4`；本文所述 A1–A11 已实施，Python 侧保持未改。
- Q1 已由用户在本实施 session 确认为 **(a)「未选择对象」**：没有目标时仍渲染禁用的共享 composer。
- Q2 已由用户确认为**按创建阶段区分**：创建、启动与准备流程保留 Squire；指代已创建 agent 的文案改用 `displayName` 或「个人助理 / Agent」等中性说法。Q3 不在本线处理。
- `AgentDirectoryRow.uri` 已补齐，目录标签统一经 `agentTargetDisplayName`；跨目录 URI 查名共用同文件的 `agentUriDisplayName`。会话与 Agent 的关联改按 `row.uri`，同时修复 §4 所述 actor/URI 比较疑点。
- link 的 `peerUri` 与 `peerLabel` 已拆分；add-link 预填、fixture 去重和拓扑节点 id 使用 `peerUri`，显示使用 `peerLabel`。
- 预览与正式入口继续复用既有共享组件：`features/onboarding/ConversationStep.tsx`、`features/agent-chat/`、`features/agents/`、`features/chat-channels/` 与 `features/org/`；未新增第二套页面、私有传输或后端契约。

### 验证记录

- `cd gui && npx vitest run <§6 焦点文件及 live/first-run-live-composition.test.tsx>`：PASS，11 文件、121 用例。
- `cd gui && npm test`：PASS，157 文件、1667 用例通过，1 个既有跳过。
- `cd gui && npm run typecheck && npm run build`：PASS；Vite 仅输出既有配置与 chunk-size 警告。
- `real-daemon-loopback.test.ts`：PASS，3 用例；`contract-mock-loopback.test.ts`：PASS，2 用例。隔离环境使用独立 Python 3.12 + jsonschema，不读取生产 daemon 或用户配置。
- `node tests/dev01-onboarding-browser.mjs`：PASS，系统 Chrome，375/760/1440 三视口；`node tests/modules/dev04/org-rework-browser.mjs`：PASS，含添加/收回 link 的双视口路径。
- `node tests/auth-browser.mjs`：BLOCKED，当前 M1 `scripts/serve.mjs` 明确拒绝账号认证与首次设置（`account authentication and first-run setup are not implemented in the M1 GUI`）；未将此历史入口标为通过。
- `uv run ruff check .`、`check_structural_limits.py`、`check_module_boundaries.py`：PASS。
- 真实安装态手工 rename / 清空 displayName、原生壳与真机：UNKNOWN，本次未执行；fixture、loopback 与浏览器证据不冒充这些验收。

- 合并最终 head 的 `uv run --with libcst==1.9.0 pytest tests/ -n 4 --dist=loadfile`：21326 passed / 67 skipped / 7 xfailed / 2 xpassed，16 failed / 3 errors。3 个 IPC 环境敏感用例在 Python 3.12、短 TMPDIR 下复跑通过。`test_shared_app_against_live_and_contract_mock` 仍为 FAIL：当前宿主明确显示「当前后端尚未提供 Agent provider 请求能力」，浏览器在默认模型服务读取前终止，未进入 agent 选择；这不是 displayName 回归。
- 远端 PR GUI、声明、前置检查与 pytest-shard (4) PASS；shard 1/2/3 共 6 个失败，分别是 CI token/cancellation/cache 审计与 startup/store migration，均不在本线改动路径。首次 push 另有新分支 before SHA 全零的 CI 事件错误，实际 CLI inventory 为 PASS。
