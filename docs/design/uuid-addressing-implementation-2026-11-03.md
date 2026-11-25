# UUID 寻址实施规格（P2–P4，cloud-claude，2026-11-03）

- 状态：实施中；集成分支 `intg/uuid-addressing-20261023`，WIP PR #1886 → `release/v0.5.5.4`
- 依据：`uuid-addressing-migration-path.md` §2 决策、§4 P2–P4、§9 U1–U3、§10 X；冻结铸造点清单 `uuid-addressing-cast-sites-d05d32b9f.md`
- 本文件记录实施层的决定，供并行各线共用。设计结论有变时停下来等 HQ 裁，不在这里改。

## 1. 已定规则（来自裁定，不再讨论）

- U1 无兼容窗：发送到短名地址 fail-loud，错误里给出 id 地址。
- X：旧短名永久冻结，只作只读别名，用来解析历史记录（回执、轨迹、审计）。已用过的短名不可再分配给新 agent。
- 用户在 CLI/GUI 输入名字、在边界解析成 id，属于输入便利，可以保留。
- U2：所有人都可以 rename，只改显示名。
- U3：home 行迁移复用 ensure-home 的两阶段机制。
- 设备与用户只新增输出字段（`deviceUid`、`agentId`），不改它们的路由键。

## 2. 实施决定

### 2.1 键与地址

- `agents.actor` 保持为不可变的内部键，取创建时的名字。pins、grants、blocks、home、desired-state 引用都继续以它为键，所以 rename 不需要改写任何外键。
- `agents.agent_id` 是派生列，带唯一索引（P2 已落地，`c2c442eb7`）。
- 官方地址 = `agents.uri` = `mint_agent_uri(owner, machine, actor=, agent_id=)`（`identity/impl/agents/addressing.py`）。
  - `ID_FORM_MINTING` 关闭时铸造出名字形态，打开时铸造出 `agent:<owner>:<node>:a.<id>`。
  - 各线合入期间开关保持关闭。翻转提交会同时打开开关并迁移测试，之后再删掉开关。
- 名字 → 地址只有两个入口，任何代码都不得自己用 `canonical_agent_uri(owner, node, <agent 名>)` 拼地址：
  - identity 侧：`official_agent_uri` / `AgentRegistry.uri_for`；
  - daemon 侧：`_AgentsRegistryMixin._agent_address(name, owner=None)`。
- 例外是 daemon 服务 actor（pac-expansion、dispatch、`RESERVED_AGENT_ACTOR_NAMES`）。它们没有 registry 记录，继续用名字地址。
  - squire 不属于这类例外。原稿把 squire 当成『没有 registry 记录的服务 actor』，这个假设不成立：squire 一直是 registry 里的普通 agent（`agents.create('squire')`）。Allen 2026-11-06 裁定 squire 就是普通 agent，后端不对它做任何特殊处理：它的名字地址和其他 agent 一样被 retire，destroy 之后名字冻结；daemon 自己发的通知不再以 squire 名义发送，改用 daemon 的服务身份 `dispatch-coordinator`。前端自己保存哪个 agentId 是用户的 squire。

### 2.2 显示名与冻结名

- `agents.display_name`：为 NULL 时显示名等于 `actor`。
  - `Agent.display_name` 字段，`Agent.display` 属性。
  - `to_json` 新增 `agentId`、`displayName`，只增不改。
- 名字唯一性：在活着的 agent 之间，`actor` 与 `display_name` 的并集里不能有重复。
- 冻结名台账 `agent_name_ledger(name PRIMARY KEY, agent_id, owner, machine, frozen_at_ms, reason)`：
  - 写入时机：非补偿性 destroy 时冻结 `actor` 和当前显示名；rename 时冻结旧显示名。
  - `create` 和 `rename` 遇到已冻结的名字，且属于别的 agent，就拒绝，错误码 `AGENT_NAME_FROZEN`。
  - 补偿性 destroy（saga 回滚）不冻结名字。
- 只读别名：`AgentRegistry.historical_agent_id(name)`，只供读历史使用，发送路径不用。

### 2.3 fail-loud（开关打开后生效）

在发送入口检查本节点的 `agent:` 地址：
- 段是名字，并且属于一个已登记的 agent（`actor` 或显示名）→ 拒绝，错误码 `AGENT_ADDRESS_RETIRED`，`data` 带 `target`、`address`（id 地址）、`agentId`。
- 段是已冻结的名字 → 同一错误码，`data` 带历史 `agentId`。
- 段是 id，但不是活着的 agent → `TARGET_UNRESOLVED`，fatal。
- 段是名字，但本节点没有任何东西认领 → `TARGET_UNRESOLVED`，fatal。这是 2026-10-23 事故的修复：过去这种消息会 `ACK_RECEIVED` 后停在 inbox。
- 服务 actor、未登记的 streaming harness、interactive session：继续按名字投递。

投递侧：本地 receive 对本节点 `agent:` 收件人做同样的认领判定。不被认领的消息给出终态失败，不再 ACK 后停放。这一步只通过 wiring 注入判定函数，不改 inbox 的结构（S5 属于阶段 A）。

裸名字（不带 `:`）在发送入口照旧解析成官方地址，属于输入便利。

### 2.4 P4 一次性迁移（开关打开后，daemon 启动时执行）

- identity：
  - `agents.uri` 名字形态 → id 形态，同时冻结旧的地址名；
  - `agent-home:<actor>` 行 → 按 ensure-home 两阶段机制补 `agent-home:<id>`；
  - `agent_secret_grants`、`retired_session_refs` 加 `agent_id` 列并回填。
- 外部存储里的本地 agent 名字 URI → 官方 URI，映射由 registry 给出：adapters 路由绑定、routines actor 字段、desired-state interactive sessions、PAC 持久化 owner/actor。
- 不回填：inbox/custody/delivery_status/dlq 的存量消息行（裁定 X）。

## 3. 并行分工（按文件所有权划分，避免冲突）

| 线 | 所有权 | 内容 |
|---|---|---|
| I identity | `identity/impl/agents/**`、kernel `ipc_errors` 中的 `AGENT_NAME_FROZEN` | §2.2 全部；`create*`/rotate 改走 `mint_agent_uri`；§2.4 的 identity 部分 |
| A 铸造点 | daemon 里除 B、C、D 所有文件之外的铸造点；`shell/.../interactive.py`；presence | 冻结清单里 daemon/shell 的 agent-name 铸造点改走 `_agent_address`/`uri_for`；presence/liveliness/路由注册在开关打开时用 id 宣告 |
| B fail-loud | `messaging/inbox_surface/send.py`、`messaging/delivery/sends.py`、`alias_resolver.py`、`agents_registry.py` 的解析函数、本地投递判定的 wiring、kernel `AGENT_ADDRESS_RETIRED` | §2.3 |
| C 对外面 | `agents_registry._agent_status_json`、targets 行、device/roster/org directory 输出、`request_catalog/agent`、openapi、`contract/`、CLI 列表渲染 | 输出新增 `agentId`/`deviceUid`；`agent list` 显示为 `短名(a.xxx)` |
| D 存储迁移 | adapters、routines、desired-state、PAC 的持久化 URI | §2.4 外部存储部分 |
| 第二阶段 | rename 的 CLI/IPC/contract | I 线合入后进行 |
| 翻转 | 打开开关 + 测试迁移 | 所有线合入后进行 |

阶段 A（#2323）的四块：`links/`、`inbox/service/link_ledger.py`、`network/route_delivery.py`、owner link 分支。各线都不改它们的结构，只经共享解析函数改键类型，每一处都列进 #1886 正文。

## 4. 集成决定（第一轮评审后，2026-11-03）

- D1 冻结名台账保留每一个 incarnation。翻转前名字可以复用，所以同一个名字可能对应多个历史 id。台账不覆盖已销毁 incarnation 的记录；`historical_agent_id(name)` 返回最近冻结的那个 id。
- D2 不新增 `agent-home:<agent_id>` 行，home 仍以不可变的 `actor` 为键（见 §5 Q1）。第一版试过 alias 行，没有任何读方，还会让 `owner_migration` 统计的 home 数量翻倍，已撤回。
- D3 `_AGENT_ROW` 新增的 `agentId`、`displayName` 由 I 线负责，并同步重新生成 `contract/daemon-request/openapi.json`。C 线在这个基础上继续。
- D4 翻转前已入队、收件人为旧名地址的 inbox 行，翻转后照常可读、可消费：已登记 agent 的消费读取键带上自己的冻结名 URI `agent:<owner>:<node>:<actor>`。这是裁定 X 的只读别名：只读，不用于发送，也不回填。
- D5 routine 的 `produces` 与 agent 的比较一律先经 registry 解析，不做字符串比较，也不把 `parse_agent_uri(...)[2]` 当成 harness 名。存的是名字 URI 还是 id URI（D 线迁移之后），结果都一样。
- D6 desired-state 里 interactive session 的 actor，由 daemon 启动时的 `_normalize_persisted_interactive_sessions` 负责迁移，D 线不重复。前提是 registry 打开（会执行 identity 侧的 P4 迁移）发生在这一步之前。
- D7 阶段 A 相邻的键类型改动，都列进 #1886 正文：`composition/links.py`（link_top_fields），以及 `request_catalog/link.py` 中 `_actor` 改走 `_canonical_interactive_actor`。

## 5. 待 HQ 裁

- Q1（U3 的前提）：本实施让 `actor` 保持为不可变的内部键，rename 只改 `display_name`，所以 `agent-home:<actor>` 永远不需要改键。U3「home 行迁移复用 ensure-home」的前提是 rename 会改键，现在不成立。建议不新增 `agent-home:<id>` 行。如果 HQ 仍要求 home 以 id 为键，需要改动约 30 处 `agent-home:{actor}` 和 home 目录的命名，单独排期。
- Q2（未被认领的本节点短名）：翻转后，发到一个本节点无人认领的短名，从「入队并返回 TARGET_UNRESOLVED 码」改为请求层直接拒绝（TARGET_UNRESOLVED，fatal）。这修复了 10-23 事故，但改变了已发布的 `message.send` 行为。
- Q3（PAC wf-* worker）：`pac/workflows/graphs.py` 在 worker agent 创建之前就铸造了 `actor_uri`。迁移路径 §5 已把 wf-* 推迟到 PAC 图 schema 升级。本线暂不处理，请确认。

## 6. 翻转阶段的集成决定（2026-11-03，`intg/uuid-flip`）

翻转普查（开关打开、跑全量）新增 994 项失败，涉及 169 个文件。按根因修复后，又确定了下面几条：

- D8 sender 地址：不带会话的 `message.send`，如果 `from` 是已登记 agent 的名字 URI，与收件方同样处理，报 `AGENT_ADDRESS_RETIRED` 并给出 id 地址。operator 的 `--on-behalf-of` 同理，不静默改写。冻结名作为 sender 时，带历史 `agentId`。
- D9 `message.history` 与回复提醒也走 D4 只读别名：已登记 agent 在翻转前、以冻结名 URI 入队的行，读历史时能看到。只读，不作发送键。
- D10 `AGENT_EXISTS` 的提示不再说「destroy 后可复用名字」。裁定 X 下，destroy 会冻结名字。
- D11 名字还没有记录时直接 `lifecycle.start`（包括 routine coordinator），在创建出记录后按记录的官方地址重新推导 spec 和 route。思路与会话首次注册的修复相同。
- D12 CLI 的 codex attach 拿 ps 行里记录的官方地址比对，不再用 CLI 自己按名字拼的 URI。
- D13 PAC wf-* worker（Q3 中能立即处理的部分）：receipt 里的 `actor_uri` 与活 agent 或会话比较时，两侧都先经 registry 解析（同 D5）。PAC GC 测试里「同名替身」改用 `rotate_secret_authority` 构造。
- D14 `stamp_agent_id` 只吞掉「列不存在」这一种 OperationalError，其他错误照常抛出。
- D15 第一轮把 destroy 遇到 incarnation 不匹配时的 `AGENT_NOT_FOUND` 改成了软返回。除非有测试证明翻转必须这样，否则恢复为原来已发布的行为。
- 测试迁移纪律：凡是验证「同名后继」性质的测试，用 `rotate_secret_authority` 或补偿性 destroy 构造同名后继，不换新名字，避免断言变成恒真。

## 7. 待 HQ 裁（补充）

- Q4（Lark 转述行）：「转述自 <完整 actor>：」（Allen 决定 4）在翻转后显示的是 `agent:<o>:<n>:a.<id>`，对人不友好。可选做法：改为显示名加 id，例如 `转述自 display(a.xxx)：`；或保持完整 actor（id URI）。当前实现保持完整 actor。
