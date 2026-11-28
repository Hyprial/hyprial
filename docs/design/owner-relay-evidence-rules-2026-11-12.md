# Owner 转述证据规则 v0.1（relay evidence rules）

状态：**WIP，待研发实施**。本 PR 只落规则，不含实现。

来源：
- owner verified Lark p2p `1c449642`（2026-11-12T17:37:50Z）：「同意，规则写为 WIP PR 后转告研发部开工派 PAC」。
- 前序 owner verified Lark p2p `471259dc`（2026-11-12T17:35:53Z）：要求给出「忽略恶意攻击，只防止误操作」的最简方案。
- 研发初稿（agent 交付，非授权）：`notes/rd/2026-11-12-owner-relay-attestation-proposal.md`（hyprial-cli-developer，2026-11-12T16:12:53Z）。

范围：Harness Network 内，一个 agent 把 owner 的 Lark p2p 指令转述给另一个 agent 时，接收方如何在不要求 owner 二次直发的前提下安全采信该转述。

非目标：防伪造、防恶意重放、防 owner 账号被盗、防 daemon 或 adapter 被攻破、跨组织转发。按 owner 指示，MVP 只防误操作。

## 0. 结论

转发包携带可机器校验的原始消息引用；接收方 daemon 回查自己已存的原始入站消息：存在、owner verified、p2p、原文 hash 一致、目标接收者是自己、在时效内、动作不越权。任一不满足即 fail-closed，不执行。

不引入签名、密钥、信任库或在线验证。adapter 签名是后续升级项，不是本 MVP。

## 1. 术语

- **原始消息**：owner 通过 Lark p2p 发给转发 agent 的 text 消息，由 adapter 注入 daemon。
- **转发 agent**：实际收到原始消息、并发起转述的 agent。
- **接收 agent**：收到转述的 agent。
- **verified relay**：接收 daemon 对转发包的校验结论；只有 `verified` 可作为规则依据。
- **unverified**：校验不可得或不通过；**不等于** `verified`，不得用于触发任何写操作。

## 2. 转发包（schema `hyprial.relay/v1`）

`harness_send` 增加可选字段 `relay`（MCP/CLI 同名），至少包含：

| 字段 | 类型 | 来源 / 约束 |
| --- | --- | --- |
| `schema` | string | 固定 `hyprial.relay/v1` |
| `provider` | string | 原始消息 `origin.provider`；MVP 仅 `lark` |
| `adapter` | string | 收到原始消息的 adapter 名 |
| `messageId` | string | 原始入站消息的 messageId |
| `sender` | object | 原始 `origin.sender`（含 standing）；MVP 仅接受 standing=`verified` 且为 owner |
| `chatType` | string | 原始 `origin.chatType`；MVP 仅 `p2p` |
| `platformCreatedAt` | int (ms) | 原始 `origin.platformCreatedAt` |
| `contentSha256` | string | 按 §4 规范化的原始 text 的 SHA-256（小写 hex） |
| `intendedRecipient` | string | 接收 agent 的 actor URI；必须等于本包 `to` 中唯一 agent 目标 |
| `relayedBy` | string | 转发 agent 的 actor URI；必须等于本包实际 sender |
| `relayedAtMs` | int (ms) | 转发动作的 daemon 时间，不是模型自报时间 |
| `originalText` | string | 原始 text 的逐字副本（按 §4 规范化）；不得混入转发者注释 |

要求：

1. 字段由 daemon/CLI 从原始入站记录和发送请求生成；**agent 不得手写或改写任何字段**。实现上应由 daemon 在发送时按 `messageId` 读取原始消息，而不是信任调用方传入的 `originalText` / `contentSha256`。
2. 若 MCP 层暂时无法让 daemon 读取另一 actor 的原始入站记录，则 MVP 允许调用方传引用；但接收方仍须按 §3 回查，回查不到即 `unverified`。
3. 转发者的说明文字必须放在转发包之外（普通消息正文），不得进入 `originalText`。
4. `intendedRecipient` 存在且唯一；MVP 不支持一条转述同时授权多个 agent。

## 3. 接收方校验（MUST，fail-closed）

接收 daemon 在把消息交给接收 agent 之前（或 agent 调用专用只读校验接口时）执行：

1. `schema` / 版本可识别；未知字段或未知版本 → `unverified`。
2. 在**本 daemon** 的入站消息存储中按 `messageId` 找到原始消息；找不到（含已清理、跨 daemon）→ `unverified`。
3. 原始消息 origin 满足 `provider=lark`、`chatType=p2p`、`sender.standing=verified`、sender 为 owner → 否则 `invalid`。
4. 原始消息是 text；群聊、forwarded、富文本、附件 → `unverified`。
5. `contentSha256` 与存储原文（§4 规范化）一致，且 `originalText` 与存储原文逐字一致（规范化后）→ 否则 `invalid`。
6. `intendedRecipient` 等于接收 agent 的 actor URI；转发包 `to` 中若含多个 agent 目标 → `invalid`。
7. 时效：`relayedAtMs − platformCreatedAt` 在窗口内（A 类 ≤ 24 h，B 类 ≤ 15 min）；超窗 → `expired`。
8. 请求动作 ⊆ `originalText` 明确授权，且属于 §5 允许类别；越权或无法判定 → `refused`。
9. 转述只能一跳：`relayedBy` 必须是原始消息的接收 agent；嵌套 relay → `invalid`。

结论必须以结构化字段交给 agent（例如 `relay: {state, messageId, reason}`），**不能由 agent 从文案里自行判断**；`unverified` / `invalid` / `expired` / `refused` 一律不得映射为「已验证」。

## 4. 规范化与 hash

- 仅处理 text 消息；`\r\n` → `\n`；其余字节不变（不 trim、不补空格、不做 Unicode 归一化）。
- `contentSha256 = sha256(utf8(normalized_text))`，小写 hex。
- 校验时对存储原文与 `originalText` 分别重算并比较；任一不一致即 `invalid`。
- 平台转义（如 Lark JSON text）必须由 adapter 先还原成用户可见文本再计算；具体解析规则在实现 PR 中固定并测试。

## 5. 允许范围（A / B / C）

| 类 | 内容 | relay 是否可用 |
| --- | --- | --- |
| A 信息 / 只读 | 汇报、查询、读操作、提供上下文 | 可用；不触发写操作 |
| B 低风险 | owner 明确列入 allowlist 的具体操作 | 默认关闭；allowlist 为空时一律拒绝 |
| C 高风险 | 凭据/权限/密钥、agent 生命周期（启停/重启/改配置）、生产部署/回滚、删除/force-push/分支保护或 required checks 变更、PR/仓库写、资金/spend、org/owner 变更、代表 owner 对外发送 | **仅 owner 直发**；relay 只能作背景，不能作授权 |

MVP 默认：A 可用；B 为空；C 不变。任何扩权解释都按 C 处理。

## 6. 时效

- A 类：自原始消息平台时间起 24 小时内。
- B 类：自原始消息平台时间起 15 分钟内。
- 原始消息已从存储清理 → 视为过期/不可验证，不执行。
- 时间以 daemon 记录的入站/转发时间为准；agent 自报的 `relayedAtMs` 不能作为唯一依据。

## 7. 安全边界（非目标）

本规则防：转述走样、错发接收者、使用过期旧指令、把 C 类塞进转述、嵌套转发、未知或损坏证据被当成授权。

本规则不防：恶意构造转发包、跨接收者的恶意重放、owner 账号被盗、daemon/存储被攻破、adapter 被攻破。因此 C 类永不走 relay；B 类 allowlist 由 owner 控制。

后续升级：adapter Ed25519 离线签名（研发初稿），可把信任锚从同 daemon 存储提升到跨信任域；本 PR 不做。

## 8. 与现有机制的关系

- 现有 owner authority 判定继续只认 owner 直发；本规则不改变它，只新增一种 `verified relay` 输入。
- 入站消息的 origin 已携带 `provider` / `chatType` / `sender.standing` / `platformCreatedAt`（见 `src/hyprial/daemon/impl/application/messaging/visibility.py`）；校验复用这些字段，不新增第二套 sender 判定。
- 转发包通过 `harness_send` 发送；旧接收方不认识 `relay` 字段时必须忽略且不得据此授权（fail-closed）。

## 9. 验收（每格须能变红）

| # | 场景 | 期望 |
| --- | --- | --- |
| 1 | A 类 verified relay | `verified`，执行只读操作 |
| 2 | 缺失 messageId / 查不到 | `unverified`，不执行 |
| 3 | sender 非 owner 或 standing≠verified | `invalid` |
| 4 | chatType=group | `unverified` |
| 5 | hash 不一致 / originalText 被改写 | `invalid` |
| 6 | intendedRecipient 非本 agent | `invalid` |
| 7 | 超时效窗口 | `expired` |
| 8 | C 类经 relay | `refused`，提示 owner 直发 |
| 9 | 嵌套 relay（relay of relay） | `invalid` |
| 10 | 未知 schema 版本 / 旧接收方 | 不执行 |
| 11 | 原始消息已清理 | `unverified` |
| 12 | 正常直发消息 | 不受影响 |

## 10. 实施建议与工作量（研发评估为准）

- daemon：入站存储按 `messageId` 的只读回查（跨 actor，仅返回断言：found / origin / hash-match，不返回 body）；`harness_send` 的 relay 字段生成；接收侧校验与结构化结果。
- CLI / MCP：展示 `relay.state`；可选提供只读 `message relay-verify` 或等价接口。
- 测试：覆盖 §9 的 12 格。
- 估算：1–3 人日。前提已由研发只读核查（2026-11-12T17:51Z）确认：同一 daemon 的 actor 共用 `state_dir/inbox.sqlite3`；`inbox.message_id` 有唯一索引，可跨 actor 点查；无需 schema 迁移；已消费行不清理（仅重投递时重建，内容不变），24h 回查可行。
- `relay.messageId` 必须使用 Harness 入站 message_id，不能使用 Lark 原生 `om_...`；实现 PR 的 contract 需固定这一点。
- 试点：先限同一 daemon（HR ↔ infra-ops，均在 [redacted]）；跨主机 / 跨 daemon 一律 `unverified`，待后续升级。

## 11. 待确认

- B 类 allowlist 的具体操作项（owner 定）。
- A / B 时效是否采用 §6 默认值。
- 回查接口的隐私边界（只返回断言，不返回原文）需在实现 PR 的 contract 中固定。
