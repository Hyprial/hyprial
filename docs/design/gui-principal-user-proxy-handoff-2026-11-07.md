# GUI synthetic operator 退场：交接与最终裁定（2026-11-08）

## 0. 最终裁定与实现

Allen 2026-11-08 在讨论当前 user-proxy 逻辑后裁定：**直接移除 synthetic `agent:<owner>:<node>:operator`，GUI 人的 principal 统一为 `user:<owner>`，不自动创建 user-proxy。** user-proxy 保持既有独立投递能力，不作为 GUI admission 前提。

配套裁定：

- 消息 `message.send/query/history/progress/lookup` 保持 `user:<owner>`；proxy 在线时回复同时转发到本人 Lark DM 是预期现状。
- 组织页管理页面选中的真实 agent，而不是 proxy 或固定宿主 agent。
- 不引入每节点自动 proxy、inbox-only proxy、跨节点 proxy 或 adapter 共享改造。

实现分支为 `intg/gui-principal-user-proxy`，同步基线 `release/v0.5.5.4` @ `ff02fc5ca0`。实现内容：

1. daemon 与 Contract Mock admission 均下发 `user:<owner>`；Node host 另行接收并校验可信 `HYPRIAL_NODE_ID`。
2. `local-operator-selector` 退场。link 读由 host 注入 `actor=user:<owner>`，`from` 是所选 agent；link 写由 host 注入 `from=user:<owner>`，浏览器提交 `onBehalfOf=<所选 agent>` 作为业务目标，daemon 继续执行 owner/onBehalfOf 授权。
3. GUI 组织页直接读写所选 agent，不再做固定 principal 的预检；chat 的本人身份也是 `user:<owner>`。
4. squire localStorage 新键按 `user:<owner>` 分区，首次读取复制旧 `agent:<owner>:<node>:operator` 键中的 agentId。
5. OpenAPI、GUI client 和 Contract Mock 示例均由声明重新生成。

以下为 2026-11-07 原始调研，保留决策过程；其中 user-proxy 主路径、A1–A3 与待裁问题**不再是实施方向**。原始代码行号以 `baf3f86b1` 为准。

- 上游：`docs/design/uuid-addressing-handoff-2026-11-04.md` §0 的「剩余的后续工作 2：operator synthetic principal 移除提案」就是本单。
- 记法：**【已核】** 表示有代码出处；**【推断】** 表示从代码读出来、但没有在真实 daemon 上跑过；**【待裁】** 表示需要 owner 决定。

## 0.1 原始裁定与目标

Allen 2026-11-07：「坐在电脑前操作 GUI 的那个人」应当以**登录 daemon 的那个 user 的 user-proxy** 作为指代，取代现在合成出来的 `agent:<owner>:<node>:operator`。该方向由 2026-11-08 最终裁定取代。

原始目标：
1. GUI admission 不再铸造 `operator` 这个名字。principal 来自 registry/profile 里真实存在的 user-proxy agent，用它的官方地址（id 形态 `agent:<owner>:<node>:a.<id>`）。
2. 所有消费 principal 的地方（request-http 的 selector 注入、`onBehalfOf`、GUI 前端的 chat/org/squire 存储）语义一致。
3. 消除 UUID 时代 `operator` 名字与真实 agent 撞名的风险（§6）。

## 1. 交接 prompt（直接转发给接手的 session）

```
你接手 hyprial 的「GUI 操作者 principal 改为 user-proxy」。向 Allen 汇报。
裁定（Allen 2026-11-07）：「坐在电脑前操作 GUI 的那个人」以登录 daemon 的那个 user 的 user-proxy 指代，
取代 GUI admission 现在合成的 agent:<owner>:<node>:operator（src/hyprial/shell/impl/gui/backend/admission.py:111、:159）。

0. 仓库与身份
- 仓库 https://code.hyprial.com/HyprialOS/harness-bridge ，先读 AGENTS.md。
- 基线 release/v0.5.5.4 @ baf3f86b1。工作分支 feat/gui-principal-user-proxy（WIP PR → release/v0.5.5.4），
  目前只有 docs/design/gui-principal-user-proxy-handoff-2026-11-07.md。
- 提交身份：cloud-claude <cloud-claude@agents.hyprial.com>。禁止 force-push 和改写历史；同步 release 用 merge。
- CI 规则：进 release/* 的 GUI 组件改动必须来自 intg/gui* 分支（scripts/ci/ci-plan.mjs）。
  本单同时改 shell/daemon 和 gui/，实现时按 UUID 那次的做法拆成两条：非 GUI 一条、intg/gui-* 叠在其上一条，
  只经 GUI PR 合入（见 uuid-addressing-handoff-2026-11-04.md §0）。

1. 先读
- 本文件全文，尤其 §4 选项、§7 待裁问题。
- docs/design/design-user-proxy-harness.md（user-proxy 的用途裁定，Allen 2026-09-23）
- contract/workers/user-proxy/README.md（转发规则）
- contract/identity/agent-addressing.md（id 地址、AGENT_ADDRESS_RETIRED）

2. 第一步：拿 §7 的 Q1–Q5 找 Allen 裁定。Q1（没有 user-proxy 时怎么办）和 Q2（哪些方法用 user、哪些用 user-proxy）
   不裁定就不要动代码。
3. 裁定后按 §8 的迁移顺序实施：先写失败的测试；改 JSON 字段或错误码同步 contract/；
   重新生成 openapi / GUI client / 固定快照（坑点见 uuid-addressing-handoff-2026-11-04.md §6）。
4. 已定规则（不要再问）：
- squire 是普通 agent，GUI 按自己保存的 agentId 认 squire（Allen 2026-11-06）。
- agent 一律以 id 地址 agent:<owner>:<node>:a.<id> 寻址，名字 URI 报 AGENT_ADDRESS_RETIRED（裁定 U1，见 contract/identity/agent-addressing.md）。
- 浏览器永远不能自报身份；principal 只由 host 注入（现有 request-http 设计）。
```

## 2. 现状：synthetic principal 在哪里铸造、在哪里消费【已核】

### 2.1 铸造与校验（shell 侧）

| 位置 | 内容 |
|---|---|
| `src/hyprial/shell/impl/gui/backend/admission.py:111` | Contract Mock：`canonical_agent_uri(MOCK_OWNER, MOCK_NODE, "operator")`，即 `agent:contract-mock:local:operator`（`MOCK_OWNER`/`MOCK_NODE` 在 `:28-29`） |
| `admission.py:159` | 真实 daemon：`canonical_agent_uri(owner, node, "operator")`；owner 来自 `node_owner_or_none`（`:148`），node 来自 `resolve_node_id` |
| `admission.py:112-117`、`:162-168` | `HYPRIAL_GUI_PRINCIPAL` 可选；给了就必须等于上面合成的值，否则 `GUI_PRINCIPAL_MISMATCH` |
| `gui/backend/source.py:231-234` | 写 request-http 的 server config：`principal = {kind: "local-operator", owner, actor}` |
| `source.py:349`、`:381-383` | 读回 `server.json`，要求 `principal.kind == "local-operator"`，owner 和 actor 与 admission 一致 |
| `source.py:502` | 把 principal 作为 `HYPRIAL_GUI_PRINCIPAL` 交给 Node GUI host |
| `gui/backend/lifecycle.py:149` | `replace_backend(actor=None)` 默认沿用 `admission.principal`。**已有换 actor 重启 request-http 的通道**（`:119`、`transport/replacement.py:57`），可用于会话中途 user-proxy 才出现的情况。但 `src/` 里目前没有调用方（只有 `tests/integration/request_http/test_lifecycle.py` 用默认 actor 调）；且 Node GUI host 的 `HYPRIAL_GUI_PRINCIPAL` 在启动时固定（`source.py:502`、`config.ts:103`），换 actor 后 `upstream.ts:193` 会报 `PRINCIPAL_MISMATCH`，所以重绑还要让 Node host 一起换 principal【推断】 |
| `lifecycle.py:394` | `TransportSession.create(actor=admission.principal)`，transport 身份绑定 principal |

### 2.2 request-http（shell 侧 HTTP 入口）

| 位置 | 内容 |
|---|---|
| `request_http/config.py:179-193` | `principal.actor` 必须是 canonical `agent:` URI，且 owner 段等于 `principal.owner`；`user:` 会被拒 |
| `config.py:278-284` | `principal` 只允许键 `kind/owner/actor`，`kind` 只能是 `local-operator` |
| `request_http/server.py:189-194` | 启动时只核对 actor 的 owner/node 段等于 daemon 的，**不核对 registry 里有没有这个 agent** |
| `server.py:240-244`、`:511-515` | 前者是写给 shell 的 `server.json`（`_write_capability_file`），后者是 `_catalog` 回给 GUI 的 `principal`；两处都是 `kind: local-operator` + `actor` |
| `server.py:574-575` | `local-operator-selector`：把 `config.actor` 注入 selector 参数 |
| `server.py:576-584` | `local-owner-selector`、`local-owner-user-selector`：注入 `user:<owner>`，与 actor 无关 |
| `server.py:604-606` | spec 声明了 `onBehalfOf` 绑定时，注入 `config.actor` |
| `server.py:607-620` | selector 与注入值不一致 → `PRINCIPAL_MISMATCH` 403 |

### 2.3 daemon 侧 spec

| 位置 | selector / 绑定 |
|---|---|
| `request_catalog/link.py:232`、`:251` | `link.list`、`link.stats`：`local-operator-selector` on `from` → `from = operator` |
| `link.py:270-272`、`:290-292` | `link.add`、`link.drop`：`local-owner-selector` on `from`（= `user:<owner>`）+ `onBehalfOf` = `config.actor`（= operator） |
| `link.py:308-322` | `link.list/stats` 经 `_actor` → `_canonical_interactive_actor` 规范化 `from`；`link.add/drop` 经 `_admin_actor`（`:345-355`）→ `_authenticated_message_caller` 解析 `onBehalfOf`，再由 `_link_actor` 只查 owner/node 段，不查是否登记 |
| `request_catalog/daemon/catalog.py:38-40`、`kernel/impl/contracts/daemon/method_spec.py:59-61` | selector 枚举 |
| `shell/impl/http_contract/projection.py:152-163` | `local-operator-selector` 的参数必须 required |
| `request_catalog/message/send/specs.py:247-248`（及 `:351`）、`message/query.py:69`、`message/history.py:73`、`message/progress/__init__.py:98`、`message_lookup.py:88` | 消息类全部是 `local-owner-user-selector` → `actor = user:<owner>`，与 operator 无关 |
| `request_catalog/identity/write_specs.py:29` | `confirmedBy` 用 `local-owner-selector` |
| `request_catalog/org/orgfs/write_specs.py:1-7` | docstring 说 export/import 是 `local-operator-selector`，实际 `:425`、`:432` 是 `local-owner-selector`。**docstring 过时**，顺手改 |
| `request_catalog/link_examples/__init__.py:7` → `contract/daemon-request/openapi.json:17485` 等、`gui/src/features/agent-provider/api/examples.generated.json:1916` 等、`tests/daemon/contract_mock/example_integration/test_link_producers.py:12` | 示例里的 `agent:demo@example.test:demo-node:operator` |

### 2.4 GUI server（Node）

| 位置 | 内容 |
|---|---|
| `gui/server/daemon-request/config.ts:38`、`:56-63`、`:103` | `HYPRIAL_GUI_PRINCIPAL` 必须匹配 `^agent:owner:node:actor$` |
| `gui/server/daemon-request/proxy.ts:141-145`、`:302-307` | 前者把 `principal{owner,node,actor}` 交给浏览器会话（`sessionResponseBody`）；后者是 Contract Mock catalog 的 `principal`（`kind: 'local-operator'`） |
| `gui/server/daemon-request/upstream.ts:174`、`:193` | catalog 的 principal 必须与 env 一致，否则 `PRINCIPAL_MISMATCH` |
| `gui/server/live/process.mjs:7-13`、`src/hyprial/shell/impl/gui/live/runtime.py:52-54`、`contract/gui-live/README.md:9`、`contract/e2e/gui_live/run.py:120` | 旧的 live host 路径：`process.mjs` 只要求 `HYPRIAL_GUI_PRINCIPAL` 存在并透传给 Python 子进程，`runtime.py:52-54` 要求它等于 `user:<owner>`（e2e 用 `user:e2e`），**与 daemon-request host 的 agent URI 要求互相矛盾**；是否仍在用未核实 |
| `docs/guides/gui-dev-mode.md:323`、`:337-338`、`:371` | `:323` 写明 principal 为 `agent:<owner>:<node>:operator`；`:337-338`、`:371` 写「derived canonical actor」「canonical operator」 |

### 2.5 GUI 前端（`principalActor` 的消费者）

| 位置 | 用途 |
|---|---|
| `gui/src/app/backend/browser-session.ts:246` | `principalActor = session.principal.actor` 写进 connection descriptor |
| `gui/src/app/capabilities.tsx:93-98` | squire agentId 的 localStorage 键按 principal 分区 |
| `capabilities.tsx:123-126` | `chatSession.actor = principalActor` |
| `capabilities.tsx:127` | `createOrgLiveSource(client, principalActor)` → link 预检 |
| `gui/src/features/model/model-key-save-adapter.ts:34-35`、`:71-93` | 键为 `hyprial.gui.squireAgentId:<principalActor>`；principal 一变，已存的 squire agentId 就读不到 |
| `gui/src/features/chat-channels/connected-chat.ts:146-149` | `authorOf(from, identity)`：`from === identity` 才算「我」 |
| `connected-chat.ts:352`、`:463` | 没有 identity 就不能发送；identity 来自 `chatSession.actor` |
| `gui/src/pages/ConnectedChatPage.tsx:92-94` | `sendable` 要求 `chatSession.actor` 非空 |
| `gui/src/features/chat-channels/ConnectedChatSendPicker.tsx:19`、`:27-38` | identity 只作清空作用域，不当作认证身份 |
| `gui/src/features/org/api/link-request-api.ts:6-11`、`:95-130` | link 预检：`link.list({from: principal})`，要求 `result.actor === 所选 agentId`，否则 fail closed，零写请求 |
| `gui/tests/first-run-browser-contract.mjs:34`、`gui/tests/daemon-request-host/real-daemon-loopback.test.ts` | 测试里硬编码 `:operator` |

## 3. user-proxy 现状【已核】

- **是什么**（`design-user-proxy-harness.md` §1，Allen 2026-09-23）：每人一个的 agent，程序是 hyprial 打包的 python 转发 worker（`daemon/impl/harnesses/python_worker/_user_proxy_worker.py`），有**自己专用的 Lark adapter**。转发规则（`contract/workers/user-proxy/README.md`）：来自本人 Lark DM 的 `@<收件人> 文本` 以 proxy 身份转发出去；**其他任何人发给 proxy 的消息，都原样转发到本人的 DM route**。
- **怎么认出来**：`users.json` 的 profile 字段 `deliveryAgent`（`identity/impl/users/profile.py:181`、`:263-265`），由 `hyprial user-proxy setup` 写入（`shell/.../squire/user_proxy.py:289`）。存的是**名字**（默认 `<ownerKey>-proxy`，`:106-108`），不是 agentId。daemon 用 `agents.get(name)` 得到 URI（`daemon/impl/application/shutdown.py:907-913`），用 desired-state 里 `harness == "user-proxy" and name == agent` 判断是否 live（`application/messaging/delivery/sends.py:264-290`）。
- **不是每个登录用户都有**。前提条件全部满足才会有：
  1. 已有本人的 users.json profile，否则 `no local user profile; run hyprial squire setup first`（`user_proxy.py:99-103`）；
  2. 一个与 squire 不同的、专用的 Lark adapter 和 route（`:110-164`）；
  3. 必须在 profile 的 preferred-receiver 节点上运行（`:166-174`）；
  4. 首次必须从 CLI 启动，daemon 才有它的 launch intent（`application/actors/agent_start/__init__.py:101-106`）。
  GUI 有 `UserProxySection`（`gui/src/features/user-settings/user-proxy/`），但 `createConnectedGuiCapabilities` 没有接 `userProxy`（`capabilities.tsx:62` 只声明了可选槽位），后端也没有对应方法。**GUI 目前无法创建 user-proxy**。
- **「登录 daemon 的 user」**：daemon 只有一个 owner，`HYPRIAL_OWNER` > `settings.json.owner`（`identity/impl/onboarding/identity_record.py:70-84`、`:86-134`）；正常路径由 `hyprial login` 写入。users.json 里查 profile 用 `UserProfileStore.resolve(owner)`，owner key 优先，其次 owner（`profile.py:356-383`）。GUI admission 已经用的就是这个 owner（`admission.py:148`）。
- **`user:<owner>` 与 user-proxy 在投递上已是同一个人**：发给 `user:<owner>` 的消息，profile 配了 `deliveryAgent` 且 proxy live 时，走 proxy（`daemon/impl/squire/addressing.py:450-463`），否则回落到 squire adapter；`shutdown.py:907-919` 把 proxy URI 计入本人的地址集合。

## 4. 设计选项

### 4.1 principal 怎么得到

**A. admission 时解析 user-proxy 的官方地址（推荐作为主路径）**
`_admit_daemon` 在 `verify_ready_daemon` 之后：`UserProfileStore(state_dir/"users.json").resolve(owner)` → `deliveryAgent` → registry 得到 `agent.uri`（id 形态）。要求 `agent.machine == node`（request-http 启动检查 `server.py:189-194` 要求 principal 是本节点 agent）。更稳的做法是 daemon 加一个只读方法返回「本人 user-proxy 的 agentId/uri/live」，admission 不直接读 state 文件。`deliveryAgent` 建议顺带改为存 agentId，与 squire 的做法一致；注意 uuid handoff §0 明确把 user-delivery 的「持久化名字」保持不变，所以这是一项新改动，需要裁定（§7 附带项）。

没有 user-proxy 时的三种处理：

| 方案 | 结果 | 问题 |
|---|---|---|
| A1 fail closed：admission 报 `GUI_PRINCIPAL_UNAVAILABLE` | 简单、无歧义 | **首跑死锁**：GUI 首跑本身要建 squire profile，而 user-proxy 依赖 profile 和专用 Lark adapter；只用 GUI 不用 Lark 的用户永远起不来 GUI |
| A2 降级：principal 为空 | 只开放 `local-owner-*` 方法（消息、设置、首跑）；需要 agent principal 的方法（`link.*`、带 `onBehalfOf` 的）在 catalog 里标不可用；前端已有「无 principal」分支（`capabilities.tsx:123`、`link-request-api.ts:99-101`）。user-proxy 建好后用 `replace_backend(actor=…)` 重绑 | request-http config 要允许无 actor；squire 存储键不能再按 principal 分区（见 §5.4） |
| A3 自动创建 | admission 或首跑顺手建 user-proxy | 当前 worker 必须有 `--route`（`common/support.py:687-706`），没有 Lark adapter 就建不了；需要 owner 裁定「没有 route 的 inbox-only proxy」是否成立 |

建议 **A + A2**：有 proxy 就用它；没有就降级，不合成任何名字。A3 视 Q1 裁定再做。

**B. 继续合成，只换名字（不推荐）**：例如改成保留名并加入 `RESERVED_AGENT_ACTOR_NAMES`。能解决撞名，但违背裁定：principal 仍然不是一个真实的人的代理。

### 4.2 与 `user:<owner>` 的分工

| 方法族 | 现在 | 建议 |
|---|---|---|
| 消息 `message.send/query/history/progress/lookup` | `user:<owner>`（`local-owner-user-selector`） | **保持 `user:<owner>`**。理由：owner→自己的 agent 是 owner pair，免 link（`links/model.py:98-105`、`links/policy.py:156-168`）；回复投到 `user:<owner>`，再由 user-delivery 决定走 proxy 还是 squire。若改成 `onBehalfOf = proxy`，GUI 的每次对话都要 proxy↔目标的 link（`policy.py:170-178` 只豁免非 agent 发送者），而且对方回复会被 proxy worker 转发到 Lark DM |
| 本地文件、`confirmedBy`、orgfs export/import、`link.add/drop` 的 `from` | `user:<owner>`（`local-owner-selector`） | 保持 |
| `link.list/stats` 的 `from`、`link.add/drop` 的 `onBehalfOf` | operator | 改为 user-proxy 地址；但见 §5.2，组织页的真实需求可能是「替所选 agent 管 link」 |
| GUI 前端的「我」 | `principalActor`（operator） | 「我」= `{user:<owner>, proxy 地址}` 两者之一；host 应同时下发两者 |

## 5. 影响面

### 5.1 request-http config 与 selector
- `principal` 形状：建议改为 `{kind: "local-user", owner, user: "user:<owner>", proxy: "agent:…:a.<id>" | null}`，或者保留 `kind/owner/actor`，只让 `actor` 可为 null。`config.py:179-193`、`:278-284`，`source.py:231-234`、`:381-383`，`config.ts:56-63`，`proxy.ts:141-144`、`:302-306`，`upstream.ts:193` 同步改。`kind` 值改动属于 breaking，PR 正文要有 `Contract-Impact:` 行。
- `local-operator-selector` 建议改名为 `local-proxy-selector`（或 `local-principal-selector`），值为 proxy 地址；proxy 为 null 时，该类方法 `HTTP_NOT_READY`/`PRINCIPAL_UNAVAILABLE`。涉及 `method_spec.py:59-61`、`:548-550`，`catalog.py:38-40`，`projection.py:46`、`:152-163`，`server.py:574-575`、`:604-606`，以及 openapi 和 GUI client 重新生成。
- `server.py:189-194` 应补一条：principal 必须是 registry 里**已登记、harness 为 user-proxy** 的 agent，而不只是 owner/node 段对得上。

### 5.2 link 治理
- user-proxy 是真实 agent，有真实的 link 文件和 k 预算；operator 没有登记，`link.list` 返回空表，`link.add` 会给一个不存在的 agent 写 link 引用（`link.py:312-322` 只校验段）【推断】。改为 proxy 后，GUI 写的是 proxy 的真实 link。
- 组织页现状【推断】：预检要求 `link.list` 的 `actor` 等于所选 agent（`link-request-api.ts:113-122`），而 operator 永远不等于任何真实 agent，所以 GUI 对任何真实 agent 的 link 写都 fail closed。换成 proxy 后，GUI 只能管理 **proxy 自己的** link。若产品要的是「人替自己的任一 agent 管 link」，正确做法是 `from = user:<owner>` + `onBehalfOf = 所选 agent`（owner 授权），这与 principal 无关（Q3）。

### 5.3 chat 归属
- 发送用 `user:<owner>`（`send/specs.py:247-248`），前端却用 operator 判断「我」（`connected-chat.ts:146-149`）。按代码推断，GUI 里本人发出的消息会显示成 assistant；GUI 测试一律用 `identity: 'user:owner'`（`gui/tests/chat-channels/chat-send-recovery.test.tsx:75` 等），掩盖了这个问题。**未在真实环境验证**，实施前先用 live daemon 复现。
- 改为 proxy 后，如果「我」只认 proxy，问题依旧；应按 §4.2 认两个地址。另外，proxy live 时，发给 `user:<owner>` 的回复会经 proxy 转发到 Lark DM：GUI 用 `message.history` 读会话不受影响，但「GUI 里看到」和「Lark 上也收到」会同时发生，需要 owner 确认这是预期行为（Q4）。

### 5.4 squire 的 localStorage 作用域
- 键 `hyprial.gui.squireAgentId:<principalActor>`（`model-key-save-adapter.ts:71-78`）。principal 从 operator 换成 proxy，或者从 null 变成 proxy，已存的 squire agentId 都会丢，首跑会再走一遍。
- 建议作用域改为与 principal 无关的 `user:<owner>@<node>`（或 owner + deviceUid），并一次性迁移旧键 `…:agent:<owner>:<node>:operator`。

### 5.5 Contract Mock
- `admission.py:111` 的 mock principal 需要换成 mock 场景里的一个 user-proxy agent（id 形态，例如 `agent:contract-mock:local:a.<固定id>`），mock 的 `agents/ps` 里同时要有这一行；再加一个「无 user-proxy」场景覆盖 A2 降级。
- `link_examples/__init__.py:7` 和 openapi 示例同步换掉。

### 5.6 文档与测试
- `docs/guides/gui-dev-mode.md:323`、`:337-338`、`:371`；`contract/gui-live/README.md:9`、`contract/e2e/gui_live/run.py:120`、`src/hyprial/shell/impl/gui/live/runtime.py:52-54` 的 `user:` 说法要统一。
- 仍硬编码 `:operator` 的测试：`tests/shell/gui/backend/`（`test_source_*.py`、`test_transport.py`、`test_gui_launcher_seams.py`）、`tests/shell/gui/lifecycle/test_gui_launcher.py`、`test_gui_signals.py`、`tests/shell/request_http/test_*.py`、`tests/integration/request_http/*`、`gui/tests/first-run-browser-contract.mjs:34`、`gui/tests/daemon-request-host/real-daemon-loopback.test.ts:32`、`:361`；示例派生的 `tests/daemon/contract_mock/example_integration/test_link_producers.py:12`（`git grep -c ':operator\b' -- tests gui` 可复跑）。`tests/daemon/application/startup/test_daemon_registration_attribution.py`（owner 段叫 `operator`）、`tests/scripts/test_agent_home_destroy.py`、`tests/shell/cli/agents/test_cli_agent_migration.py`（`user:operator`）、`tests/daemon/identity/test_identity_resolver_ipc.py`（`manual:operator`）里的 `operator` 是别的含义，不要误改。

## 6. UUID 时代的撞名风险【推断，代码路径已核】

`operator` 不是保留名：`RESERVED_AGENT_ACTOR_NAMES` 只有 `dispatch-coordinator`、`_turn-hooks`（`kernel/impl/contracts/reserved_actors.py:28-30`；registry 在 `identity/impl/agents/registry/_core.py:209` 只拦这些）。用户可以建一个名叫 `operator` 的 agent。此后：
- `link.list/stats`：`from = agent:<o>:<n>:operator` 经 `_canonical_interactive_actor`（`application/messaging/visibility.py:504-542`）被解析成那个真实 agent 的 `a.<id>` 地址。**GUI 会读到别人的 link 表**。
- `link.add/drop`：`onBehalfOf` 经 `_authenticated_message_caller`（`visibility.py:240`、`:289`）→ `_resolve_on_behalf_actor`（`visibility.py:342-361`）→ `_refuse_retired_local_address`（`application/actors/address_admission.py:135-171`）报 `AGENT_ADDRESS_RETIRED`；这个 agent destroy 之后名字冻结，同样报错。GUI 的 link 写从此永久失败。
- 只要 principal 还是按名字合成的，这个风险就在；改用 proxy 的 id 地址后消失。

## 7. 待裁问题（给 Allen）

- **Q1** 没有 user-proxy 时：A1 fail closed、A2 降级（推荐）、还是 A3 自动创建？如果选 A3：是否接受没有 Lark route 的 inbox-only proxy？
- **Q2** 消息类方法是否保持 `user:<owner>`（推荐），还是也改成以 proxy 身份发出（会引入 link 治理，回复会转到 Lark）？
- **Q3** 组织页的 link 管理：只管 proxy 自己的 link，还是改为 `onBehalfOf = 所选 agent`、由 owner 授权？
- **Q4** proxy live 时，GUI 对话的回复同时转发到 Lark DM，是否是预期行为？
- **Q5** 同一 owner 的多节点：user-proxy 只能在 preferred-receiver 节点上（`user_proxy.py:166-174`），而 principal 必须是本节点 agent（`server.py:189-194`）。其他节点上的 GUI 是按 A2 降级，还是允许 principal 指向其他节点的 proxy？
- 附带（低优先）：`deliveryAgent` 是否一并改为存 agentId；旧的 `gui/server/live` host 是否删除。

## 8. 迁移与验证计划

实施顺序（每步先写失败的测试）：
1. daemon：新增只读方法，返回本人 user-proxy `{agentId, uri, node, live}`（或 null）；`deliveryAgent` 存 agentId，兼容读旧的名字。
2. shell：admission 改为解析 proxy（§4.1 A + Q1 选定的缺失策略），删掉 `canonical_agent_uri(…, "operator")` 两处；`HYPRIAL_GUI_PRINCIPAL` 改为「若给出必须等于解析结果」或直接删除。
3. request-http：config 形状、selector 改名、启动时核对 registry（§5.1）；重新生成 openapi、CLI surface、GUI client；PR 正文写 `Contract-Impact:`。
4. GUI（`intg/gui-*` 分支）：host 下发 `{user, proxy}`；`authorOf` 认两个地址；squire 存储键迁移（§5.4）；无 proxy 的降级 UI；可选接上 `UserProxySection`。
5. Contract Mock 与示例（§5.5），文档（§5.6）。

验证：
- 单元/结构：`tests/shell/gui/backend`、`tests/shell/request_http`、`tests/integration/request_http`、`gui-unit`、contract-gap 计数；`check_daemon_contract_diff.py --base-ref origin/release/v0.5.5.4`。
- 回归断言：
  - `git grep -n '"operator")' -- src/hyprial/shell/impl/gui` 为空；
  - 建一个名为 `operator` 的 agent 后，GUI 的 link 读写不受影响（§6 的反例测试）；
  - 本人 GUI 发出的消息显示为 user（§5.3 先在 live daemon 上复现，再加测试）；
  - principal 变化后 squire agentId 仍能读到。
- E2E（`.agents/skills/hyprial-e2e`）：
  - 有 proxy：GUI 首跑 → 对话 → 组织页 link 增删；
  - 无 proxy：GUI 能启动、能首跑、link 功能明确显示不可用；
  - 会话中建好 proxy 后 `replace_backend` 重绑，前端作用域清空、重新加载；
  - 存量 home 升级：旧 squire 键迁移。
