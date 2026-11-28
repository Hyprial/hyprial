# 聊天读取会话文档（slice G1）UI 交接记录

按 [UI 变更交接模板](../ui-change-handoff-template.zh-CN.md) 填写。记录放在本业务线既有 PR 记录旁；PR 正文引用本文件。

## 1. 基本信息

| 字段 | 填写 |
| --- | --- |
| Change ID | `G1`（会话计划 slice G1：GUI 聊天读取会话文档） |
| 状态 | `READY FOR REVIEW`（仅 jsdom/API 证据；浏览器与真实后端未测，见第 6 节） |
| 产品 owner | UNKNOWN（由 GUI 线评审指定） |
| 研发 owner | coding agent（slice G1 worker） |
| 平台 | `shared` |
| 页面/组件 | 普通聊天页 `gui/src/pages/ConnectedChatPage.tsx`；控制器 `gui/src/features/chat-channels/impl/connected-chat.ts` |
| 目标分支 | `dev`（分支 `feat/gui-chat-reads-conversation-document`，由 GUI 线评审，协调者不合入） |
| 基线 head | 起于 `82fbf17d85ca1a6ccac982d73ac9a061589a56e0`；已合入 origin/dev `5fc3827c5ebf0309f59ed2d6bee6e05b5afcc961`（合并提交 `66a03ebb5`，无冲突） |
| Applicable-Decision | 会话计划 2026-11-17 第 11–15 节（记录即 GUI 读模型）；`contract/daemon-request/README.md` 中 `message.send` 会话归属（2026-11-17/18） |
| 关联任务/PR | 后端已合入：`conversations.*` 读记录（RM1）、HTTP `message.send` 可省略 `conversationId`（#2930） |
| 原型/设计来源 | 无；沿用现有聊天工作区 |

## 2. 变更目标

### 2.1 用户问题

聊天页历史读 `message.history`（只读本机投递队列），不读共享的会话文档；新对话由浏览器随机生成会话 id，同一个人和同一个 Agent 会分散到多个会话，Agent 发回这个人的消息不一定出现在用户看到的那个会话里。

### 2.2 本次目标

- 历史读 `conversations.history`（会话文档），按 `conversations.list` 表头给出的 `c.` 文档 id 读取。
- 新对话发送时不带 `conversationId`，采用 daemon 返回的默认会话 id，之后的发送沿用它。
- 机制消息（`messageClass: system`）继续进入已有的折叠“任务与系统消息”展示。
- 正文未记录的消息显示一行说明，不显示空气泡。
- `recordAvailable: false` 时页面照常工作。
- 丢失应答的发送（未带 `conversationId`）仍可用 `message.send.key_lookup` 对账。

### 2.3 非目标

- 不改后端、契约、生成 client、Contract Mock 例子。
- 不改可信身份模型（会话主体仍由服务端注入 `user:<owner>`）。
- 不新增 `recordAvailable` 的界面提示（任务允许省略）。
- 不改发送会话选择器 `ConnectedChatSendPicker` 与 `message.send.lookup`（它们只用于已存在、带 id 的会话）。

### 2.4 成功标准

1. 见第 6 节焦点测试：历史经 `conversations.history`、三种 id 映射、首发省略 id 并采用返回值、二发沿用、Agent 消息显示、system 折叠、空正文说明、`recordAvailable: false`、旧随机 id 会话可列出可读、key lookup 对账。
2. 浏览器与真实后端：NOT_RUN / BLOCKED（第 6、7 节）。

## 3. 现状基线与证据

| 项目 | 当前事实 | 证据 |
| --- | --- | --- |
| 当前页面/组件 | `ConnectedChatPage` → `AgentChatSection`；控制器 `ConnectedChatController` | 基线源码 |
| 当前数据来源 | real（daemon request）：`targets`、`message.query`、`conversations.list`、`message.history`、`message.send`、`message.send.key_lookup` | `connected-chat.ts` 决策块 |
| 当前缺口 | 历史只读队列；新对话 `crypto.randomUUID()`；`conversations.list` 的 `c.` id 被读取 API 丢弃 | `message-read-api.ts` 基线 |

## 4. 设计说明

### 4.1 页面结构

页面结构不变：聊天对象列表、会话头（刷新、会话选择器、加载更多）、消息日志、输入框。

### 4.1.1 单套 GUI 交付检查

- 预览与正式入口：同一 `ConnectedChatPage` / `AgentChatSection` / `AgentHistoryField`；只有数据源不同（真实 daemon 或浏览器内 Contract Mock）。
- 数据源、控制器与能力差异：只改控制器 `connected-chat.ts`、读取适配 `message-read-api.ts`、发送适配 `daemon-api/chat-send.ts`；展示层只在 `AgentHistoryField` 加“正文未记录”一行（预览与正式共用）。未新增页面、传输或请求路径。
- 双入口验证：均未在浏览器运行（NOT RUN）。Contract Mock 入口在 jsdom 中经 `connectContractMock` 跑通文档历史（`conversation-document-contract-mock.test.tsx`）；首发经 mock 只能证明线上一半，见 7.3。
- 生产隔离：未新增 mock 数据或开发工具；`gui/src` 未手写应答。
- 临时迁移例外：无。

### 4.2 视觉规则

| 项目 | 本次取值 | 来源 |
| --- | --- | --- |
| “正文未记录”一行 | `--ui-ink-3`、`--ui-text-sm`、斜体 | `tokens.css` 既有 token |
| 其他 | 不变 | — |

### 4.3 状态矩阵

| 场景 | 期望展示 | 可用动作 | 是否真实 |
| --- | --- | --- | --- |
| 有表头的会话 | 读文档尾页，2 秒刷新按 `after` 游标增量合并，按消息 id 去重 | 刷新、加载更多、切换会话、发送 | jsdom |
| 新对话首发 | 条目保持 `unread`（不是 `ready`）：显示本地已发送消息和“历史尚未读取，这里只显示你刚发送的消息。”（PR #2937 评审建议的措辞；按内容选择：聊天不是 `ready` 且每一行都是本标签页刚发送的消息时显示，`unread`、`error`、`loading` 都适用；其余情况保留“当前显示上次取得的消息。”），不显示“暂无消息”与“加载更多”；后台找到文档并读到尾页后才变为 `ready`，按 id 替换本地副本 | 发送、刷新消息 | jsdom（含页面渲染） |
| 首发后目录尚未列出文档时重新打开该对象 | 保留本地消息，仍为 `unread`；重新打开给后台重读一份新的次数预算 | 发送 | jsdom |
| 只有 `message.query` 发现、目录未列出的会话 | `unread`：不显示文字，不声称空历史或无会话 | 发送（沿用其 id） | jsdom |
| 用户打开聊天时目录重读失败（非拒绝） | `error`：“历史消息加载失败。”加“会话目录读取失败：<代码>”，不显示成空聊天 | 刷新消息 | jsdom（含页面渲染） |
| 用户打开聊天时目录重读被拒 | 交给 `reportUnavailable`（主题“读取消息”），条目按被拒的历史读取处理 | — | jsdom |
| 首发后后台重读目录 | 静默，最多 6 次，间隔不短于 2、4、8、16、32 秒；方法被拒（或启动时目录已被拒）立即停止且不提示；6 次后显示 `error`：最后一次失败的文字，或“会话目录里还没有这段会话，暂时读不到历史消息。可点“刷新消息”重试。”；之后点“刷新消息”、重新打开，或在 `error` 状态下发送一条被接受的消息（该发送算一次用户引起的读取，每次发送恰好一次、不留本地已发消息以外的状态变化），读到文档后变为 `ready` 并清除错误，没有定时重读；`ready`、`unread`、`loading` 下发送与未被接受的发送不触发读取，读取失败时已发消息仍显示 | 刷新消息 | jsdom（假定时器，经真实 `useConnectedChat` 计时） |
| 同一对端在同一发送 id 下列出两个表头 | 取 `lastMessageAtMs` 最大的那个（并列取先列出的）；会话选择器只列这一条 | — | jsdom |
| 正文未记录（条目带 `bodyOmitted`） | 作者、时间照常；正文位置显示“这条消息的正文没有记录在会话中。” | — | jsdom（含 Contract Mock `body-omitted` 例子经读取 API） |
| system 条目带 `bodyOmitted` | 仍折叠在“任务与系统消息”；展开后显示同一行说明 | 展开 | jsdom |
| `message` 为空但无 `bodyOmitted` | 与其他消息相同（空正文），不声称未记录 | — | jsdom |
| system 消息 | 既有折叠“任务与系统消息” | 展开 | jsdom |
| `recordAvailable: false` | 无变化 | — | jsdom |
| 发送结果未知 | 既有恢复行；`key_lookup` 返回存储的 id，选中派发目标并读其文档 | 查询原消息结果、放弃操作键 | jsdom |

### 4.4 响应式

未改布局；新一行沿用消息气泡宽度和换行规则。UNKNOWN（未做浏览器检查）。

### 4.5 键盘与无障碍

未新增交互控件。新一行是普通段落，处在 `role=log` 内。其余项 UNKNOWN（未检查）。

### 4.6 文案与数据

| 项 | 文案/来源 |
| --- | --- |
| 正文未记录 | “这条消息的正文没有记录在会话中。”（所有原因共用一行，不按 `redacted`/`redaction-invalid`/`too-large` 区分；未新增文案，账本未变） |
| 首发后台重读用尽 | “会话目录里还没有这段会话，暂时读不到历史消息。可点“刷新消息”重试。”（PR #2936 评审跟进新增，PR #2937 评审把原句“会话目录没有列出该会话（已重读 6 次）”改成此句，去掉实现细节；显示在既有“历史消息加载失败。”之下；不含账本词汇，账本未变） |
| 未读聊天只有本地消息 | “历史尚未读取，这里只显示你刚发送的消息。”（PR #2937 评审建议；不含账本词汇，账本未变） |
| 打开聊天时目录读取失败 | “会话目录读取失败：<代码>”（沿用启动时目录失败的既有前缀） |
| 其他 | 不变 |

合入 dev 后生成类型中历史条目带可选 `bodyOmitted`（原因字符串）。“正文未记录”只由该字段决定；读取 API 把它当作可选的非空字符串校验并透传。空 `message` 而无 `bodyOmitted` 的条目按原样显示。

## 5. 实现边界

### 5.1 修改文件

- `gui/src/features/chat-channels/impl/message-read-api.ts`：`history` 改读 `conversations.history`（参数为文档 id）；表头带 `documentId` 与 `conversationId`（发送 id）；按已发布 schema 校验，透传 `messageClass`、`bodyOmitted`（可选非空字符串）、`recordAvailable`。
- `gui/src/features/chat-channels/impl/connected-chat.ts`：条目同时保存发送 id 与文档 id，文档按（对端, 发送 id）查找（同一发送 id 可对应每个对端各一个 cell）；首发不带 id 并采用返回值；静默目录重读；`unread`；key lookup 对账落点（多个候选时取派发目标）；按 `bodyOmitted` 标记正文未记录。删除 `newConversationId` 端口与随机 id。
- `gui/src/features/chat-channels/impl/daemon-api/chat-send.ts`：`conversationId` 可省略（参数中不出现该键）；省略时接受返回的 id，投递若带 `conversationId` 须与顶层一致；指定 id 时仍要求回显。
- `gui/src/features/chat-channels/impl/daemon-api/index.ts`：后端 client 类型加入 `conversations`。
- `gui/src/features/chat-channels/impl/MessageSessionsSection.tsx`：按文档 id 读历史；删除无人使用的 `recent` 数据源（只有发送 id，无文档可读）。
- `gui/src/state/gui-view-model.ts`：`ConversationMessage.bodyNotRecorded` 可选字段（feature 目录之外的唯一改动）。
- `gui/src/features/agent-chat/impl/AgentHistoryField.tsx`、`chat.css`：正文未记录一行（普通消息在正文位置，system 消息在折叠内）。
- `gui/src/pages/ConnectedChatPage.tsx`：仅决策块文字。
- `gui/tests/architecture/unavailable-copy.allow.ts`：删除已不存在的字符串 `conversation id source unavailable` 的账本行（账本只减不增）。
- 测试：新增 `gui/tests/chat-channels/conversation-document.test.tsx` 与 `gui/tests/chat-channels/conversation-document-contract-mock.test.tsx`（经 Contract Mock）；`gui/tests/daemon-request-host/browser-acceptance.mjs` 改为断言 `conversations.history`（未运行）；更新 chat-channels 下 7 个测试文件与 `gui/tests/daemon-request-host/app-composition.test.tsx`（历史改为列出表头并以文档 id 读取）。

### 5.2 明确不改

后端、`contract/`、生成 client、Contract Mock 引擎与例子、`ConnectedChatSendPicker`、`SingleAgentChatSend`、可信身份。

### 5.3 数据与适配器

| 动作 | 页面回调 | 适配器/命令 | 当前结果 | 正式契约 owner |
| --- | --- | --- | --- | --- |
| 读会话目录 | 控制器启动/目录重读 | `conversations.list` | 已用；现保留文档 id | 后端 |
| 读历史 | 选中/刷新/加载更多 | `conversations.history`（原 `message.history`） | jsdom 通过 | 后端 |
| 发送 | `onSend` | `message.send`（首发省略 `conversationId`） | jsdom 通过 | 后端 |
| 对账 | 查询原消息结果 | `message.send.key_lookup` | jsdom 通过 | 后端 |

## 6. 验证记录

以下结果取自合入 dev 后的 head（代码提交 `7eef26653`，本节文字所在提交之前）。

| 检查 | 命令/操作 | 结果 | 覆盖范围 |
| --- | --- | --- | --- |
| 先失败 | 新测试文件（当时 11 个用例）放在旧基线 `82fbf17d8` 的 `gui/` 导出副本上运行 | 9 failed / 2 passed | 证明新行为不在旧基线；之后补的用例未在基线上单独跑 |
| 生成物 | 合并后 `node gui/scripts/generate-reference-client.mjs` | 无差异 | |
| 类型检查 | `npm --prefix gui run typecheck` | PASS（rc 0） | |
| 模块边界 | `npm --prefix gui run check:module-boundaries` | PASS | |
| 焦点测试 | `vitest run tests/chat-channels tests/features/agent-chat tests/contract-mock tests/daemon-request-host/app-composition.test.tsx`（排除 `contract-mock/conformance`，它在全量里跑） | 24 文件 / 387 测试 PASS | jsdom |
| Contract Mock 测试 | `tests/chat-channels/conversation-document-contract-mock.test.tsx` | 3 PASS | 经 mock 引擎、产品传输与生成 client，无手写应答 |
| 全量 GUI 测试 | 先在 worktree 执行 `uv sync --extra test --python 3.12`，清除代理变量后 `vitest run --maxWorkers=2`，排除 `agents-second-browser`（package.json 本来就排除）与 `daemon-request-host/real-daemon-loopback`（启动真实 daemon） | 217 文件 PASS / 1 FAIL / 1 skipped；2800 测试 PASS / 12 FAIL | 仅 GUI |
| 失败文件对照 | `tests/playwright-cache.test.mjs` 在 origin/dev 的 `gui/`、`scripts/ci/`、`.github/workflows/gate-gui.yml` 导出副本（`git archive`，复用本 worktree 的 node_modules，跑完已删除）上运行 | 基线同样 12 FAIL / 4 PASS，失败信息相同（`dpkg --print-architecture failed`，macOS 无 dpkg） | 基线既有，本机环境 |
| Python host 测试 | 上一轮失败的 `daemon-request-host/host`、`support/python-host`、`contract-mock/conformance`、`daemon-request-host/contract-mock-loopback` | 建好 `.venv` 后全部 PASS（它们启动的是 GUI 会话 host `hyprial.shell.impl.gui.host`，不是 daemon） | 上一轮失败只因 worktree 没有 Python 环境 |
| 请求路径门 | `node scripts/ci/check_gui_request_paths.mjs --base-ref origin/dev --base-branch dev` | rc 0 | |
| mock 来源门 | `node scripts/ci/check_gui_mock_source.mjs --base-ref origin/dev --base-branch dev` | rc 0（pending 1 为基线既有账本行） | |
| 不可用文案门 | `node scripts/ci/check_gui_unavailable_copy.mjs --base-ref origin/dev --base-branch dev` | rc 0 | |
| 请求架构门 | `node scripts/ci/check_gui_request_architecture.mjs --base-ref origin/dev` | rc 0 | |
| 生成检查 | `npm run client:reference:check`、`npm run client:fixture:check` | rc 0 / rc 0 | |
| 契约影响 | `check_contract_impact.py --base origin/dev --head HEAD` | rc 0（0 candidates） | |
| PR 卫生 | `check_pr_hygiene.py merge-tree` 与 `registry-union` | rc 0 / rc 0 | |
| 构建 | `npm --prefix gui run build`（无网络） | rc 0；production copy gate 0 forbidden markers | |
| 浏览器验收脚本 | `gui/tests/daemon-request-host/browser-acceptance.mjs` 已改（见 5.1） | NOT_RUN（需要浏览器） | 改动只在 jsdom 中按同样步骤复核过（打开 worker 见“hello”、发送后见“发送结果未知”） |
| 浏览器/交互 | — | NOT_RUN | |
| 键盘/读屏 | — | NOT_RUN | |
| 原生壳/真机 | — | NOT_RUN | |
| 真实后端 | — | NOT_RUN | |

验收分层：本次只有 jsdom/API 证据（其中一部分经 Contract Mock），不代表 real backend 或浏览器通过。

## 7. 产品接受与缺口

### 7.1 产品接受

全部待验。

### 7.2 DEV-UI 登记

“正文未记录”一行是研发补齐的展示，待产品确认文案与样式；是否按 `bodyOmitted` 原因分别措辞待产品决定（新增文案须过不可用文案账本规则，账本只减不增）。

### 7.3 剩余缺口

| 缺口 | 类型 | 影响 | owner | 下一步 |
| --- | --- | --- | --- | --- |
| Contract Mock 已按契约 `x-hyprial-mock-echo` 回显 `message.send` / `conversations.post` 的 `idempotencyKey → operationId` 与 `message.send.key_lookup` / `message.send.lookup` 的 `operationKey`/`idempotencyKey → commandId`，但各示例的 `from` 为 `user:alice`（当前预览登录身份 `user:owner` 时，控制器仍按非我方消息或示例默认会话处理）；另外 mock 选例时请求缺少的选择字段与任何值都算一致，未固定用例时省略 `conversationId` 的发送会拿到 `conversation-1` 的例子 | CONTRACT / 示例夹具 | 经 Contract Mock 首发已回显调用方 `operationId` 并可被发送适配器与控制器采纳进入 `success` / 采纳 `dc.example-default-conversation`（当 `from` 与当前身份或固定用例对齐时）；省略 `conversationId` 的通用选例仍命中 `conversation-1` | 后端/契约、mock 引擎 owner | 已通过 `x-hyprial-mock-echo` 通用声明解决操作键回显；示例发送方身份差异保留为示例与预览账号之间的已知状态 |
| 预览会话主体是 `user:owner`，例子世界说的是 `user:alice` | CONTRACT / mock | 预览中自己的消息被标为对方（Agent）的消息 | mock owner | 让 `connectContractMock` 的主体与例子一致 |
| 浏览器、真实 daemon 未测；`browser-acceptance.mjs` 已改但未运行（2026-11-22 S4：已拆分为 `gui/tests/acceptance/`） | UNKNOWN | 无端到端证据 | GUI 线 | 统一通过 `npm --prefix gui run test:acceptance` 在有浏览器的环境跑该套件；在隔离 daemon 上走一遍首发、Agent 回复、刷新、key lookup |
| 新对话等待文档时的目录重读（PR #2936 评审第 2 项已修：最多 6 次、指数退避、被拒即停、之后显示失败） | IMPLEMENTATION | 每次被接受的首发最多 6 次完整目录读取（每次最多 100 页）；记录写入慢于约 1 分钟时显示失败，用户点“刷新消息”可再试 | GUI 线 | 有订阅方法后替换 |
| 同一对端在同一发送 id 下可有两个 cell（两个线程） | CONTRACT | 页面只读最新的那个文档，另一个 cell 在本页不可达（选择器的值是发送 id） | GUI 线/后端 | 若需要两者都可读，选择器须改以文档 id 为值 |
| 刷新仍是 2 秒轮询：daemon 没有订阅记录变化的方法 | CONTRACT | 新消息最多延迟一个轮询周期，另加 recorder 的写入延迟 | 后端 | 有订阅方法后替换轮询 |
| “自己的消息”按 `from === user:<owner>`（会话主体）判断 | UNKNOWN | 计划第 14 节让用户成为一个 agent；若记录条目的 `from` 改为该 agent 的地址，自己的消息会被标成对方的 | 后端/GUI 线 | 用户成为 agent 时必须同步改作者判断 |
| `recordAvailable: false` 没有任何界面提示（已登记 KP-103） | IMPLEMENTATION | 用户看不出这次历史只来自本机队列 | GUI 线/产品 | 需要时按现有状态约定加一个不惊扰的提示 |
| 真实主体下自己的消息是否标为「你」 | UNKNOWN | 未验证：真实模式取决于 `principalActor === 'user:<owner>'` 与记录条目的 `from` 一致 | GUI 线 | 见 7.4 第 7 项 |
| `gui/tests/support/formal-agents-app.tsx` 仍把 `message.history` 列为可调用读取 | IMPLEMENTATION | 无影响（该组合没有聊天对象，不读历史） | GUI 线 | 顺手清理 |

### 7.4 合入后称“完成”之前要做的验证（PR #2936 评审清单，全部 NOT DONE）

浏览器 + 隔离的真实 daemon。以下十项均未做；本次跟进只有 jsdom 证据。

| # | 验证 | 状态 |
| --- | --- | --- |
| 1 | 用 contract-mock 与 daemon 两个 driver 运行 `browser-acceptance.mjs`（2026-11-22 S4：已拆分为 `gui/tests/acceptance/`，统一通过 `npm --prefix gui run test:acceptance` 执行） | NOT DONE |
| 2 | 对一个没有对话的 agent 首次发送：请求不带 `conversationId`，采用返回的 id，目录在一两个周期内出现该头，重读停止，本地消息被记录里的那条替换而不是重复 | NOT DONE |
| 3 | agent 的回复出现在同一对话；第二次发送复用该 id | NOT DONE |
| 4 | 丢失发送应答后按幂等键查找，落到正确的聊天 | NOT DONE |
| 5 | 旧的随机 id 对话可列出、可读、可发送 | NOT DONE |
| 6 | `conversations.list` 被拒或失败时：聊天显示错误，不像空聊天，不循环 | NOT DONE（jsdom 已覆盖） |
| 7 | 真实主体下自己的消息标为「你」（未验证） | NOT DONE |
| 8 | `bodyOmitted` 条目在普通消息和折叠的系统消息里都显示那一行 | NOT DONE（jsdom 已覆盖） |
| 9 | `recordAvailable: false` 时页面可用 | NOT DONE（jsdom 已覆盖） |
| 10 | 网络面板里没有 `message.history` 调用，请求速率有界 | NOT DONE（jsdom 假定时器已覆盖速率上界） |

### 7.5 PR #2936 评审跟进（分支 `fix/gui-chat-document-review-findings`）

- 打开聊天时的目录读取失败显示为 `error`，被拒交给 `reportUnavailable`；只有后台重读静默。
- 后台重读有界（6 次、退避、被拒即停、之后显示失败）。
- 首发后在挂上文档之前保持 `unread`，发送不再把历史状态提升为 `ready`。
- 测试代码里的行内注释移入文件头决策块；控制器两处多余缩进已改。
- 浏览器验收脚本：真实 daemon 路径恢复断言“从不调用 `message.history`”；Contract Mock 路径不再断言恰好一次历史读取，理由写在该脚本文件头（2 秒刷新每拍都读尾页，次数取决于用时），改为每个请求都指向默认文档、应答一致、次数不超过“选择那一次 + 每 2 秒一次”。脚本未运行（NOT_RUN），只做了 `node --check`。
- 同一对端同一发送 id 的两个表头取最新的一个；旧的 `MessageSessionsSection` 在产品组合里不可达（`messageRead` 只由 `createConnectedGuiCapabilities` 提供，而有了连接 `AppShell` 就渲染 `ConnectedChatPage`，不渲染 `LiveChatPage`），未改。

## 8. PR 与声明

```text
Contract-Impact: none（只消费已发布的 conversations.* 与 message.send 形状，不改字段语义）
Skill-Impact: none
```
