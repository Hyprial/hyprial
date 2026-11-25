# Chat C1 发送操作键查询设计（待复审）

状态：原设计头 `1cac106467473e26adff0cc21a316d4929cc8e50` 获设计 REWORK。本稿补齐协议和验收边界，仍须独立复审；未获 PASS 前不登记、生成客户端或启用发送。基线已 merge-forward 至 `intg/gui-live-0554@524ee9685c160b5e90d20db2a599668ea9c7c96b`，前移未改变 Chat send/lookup 权威。公共 legacy 顶层未知参数兼容 B 仍 OPEN。

## 现有事实与范围

- `message.send` 仅有孤立 MethodSpec，未进入公共 registry/OpenAPI/generated client；普通 Chat 页面禁用发送。
- 已登记的 `message.send.lookup` 需要原文 `text`，逐字比对 `sender_copies.payload.message`；参数、`request-mismatch` 和错误面均保留。刷新后只有操作键的页面不能用它。
- `sender_copies` 与 outbox 在同一 daemon 事务提交。唯一有效副本只证明 durable 接受，不证明投递或对方收到。`message.history` 不含幂等键。
- C1 普通入口只发送给一个 canonical agent；IPC `message.send` 多目标语义不改。新方法只读当前 home 的 `sender_copies`，不引入第二份意图、摘要、会话或投递权威。

## 方法契约

方法名固定为 `message.send.key_lookup`。它只按操作键查询持久接受记录，不表示投递状态或自动恢复发送。`message.status` 是不同的既有投递状态查询。

MethodSpec：`owner="daemon.inbox"`、`mutates=False`、`retry="safe"`、`audience=("local-operator", "bound-agent")`、`http_exposed=True`、`confirmation="none"`、`idempotency=IdempotencySpec(mode="safe", scope="read-only-send-key-lookup")`；HTTP binding 为 `HttpBinding(IdentityBinding("local-owner-user-selector", "actor"), readiness="inbox-service")`。

参数 JSON Schema：

```json
{
  "type": "object",
  "properties": {
    "operationKey": {"type": "string", "minLength": 22, "maxLength": 128, "pattern": "^[A-Za-z0-9_-]+$"},
    "actor": {"type": "string", "minLength": 1},
    "from": {"type": "string", "minLength": 1, "x-hyprial-http-forbidden": true},
    "sessionRef": {"type": "string", "minLength": 1, "x-hyprial-http-forbidden": true}
  },
  "required": ["operationKey"],
  "additionalProperties": false
}
```

浏览器请求体只含 `operationKey`。`actor` 是宿主从已验证 principal 绑定的槽，浏览器不能自报；`from/sessionRef` 仅供受信 IPC/bound-agent，HTTP 禁止。键格式与 `message.send` 的 GUI HTTP 绑定一致：22–128 个 URL-safe 字符，不允许 `:`、空白、`%` 或 `LIKE` 转义符 `\`；`_` 合法但不能用未转义的 SQL `LIKE` 查询。浏览器用 CSPRNG 产生 32 字节并编码为无填充 base64url（43 字符）；每个新发送意图都用新键。

结果 JSON Schema 为两种互斥形状，均拒绝额外字段：

```json
{
  "oneOf": [
    {
      "type": "object",
      "properties": {
        "status": {"const": "accepted"},
        "commandId": {"type": "string", "minLength": 1},
        "conversationId": {"type": "string", "minLength": 1},
        "messageId": {"type": "string", "minLength": 1}
      },
      "required": ["status", "commandId", "conversationId", "messageId"],
      "additionalProperties": false
    },
    {
      "type": "object",
      "properties": {
        "status": {"const": "unknown"},
        "reason": {"enum": ["not-found", "invalid-record", "unavailable"]}
      },
      "required": ["status", "reason"],
      "additionalProperties": false
    }
  ]
}
```

`commandId` 是原操作键，不含 target/index。结果不含正文、sender、target、index、投递状态或 receipt。`accepted` 只表示当前认证 sender 在当前 home 对此键有**唯一、结构完整的 durable request sender copy**；不能证明原文与刷新前输入相同，也不能证明 outbox 仍在、receipt 已确认、目标已投递或对方已收到。`unknown` 是 HTTP 200 正常结果，不触发发送或自动重试。

错误面复用旧 lookup 的认证/会话错误：`INVALID_ARGUMENT`、`SENDER_UNRESOLVED`、`SENDER_NOT_AUTHENTICATED`、`InvalidAgentNameError`（仅 IPC `from` 别名）、`STALE_SESSION`、`SESSION_SUPERSEDED`、`INVALID_SESSION_SOURCE`。参数/身份错误沿现有 HTTP envelope 映射到 400/401/403；DB/OS 读取失败返回 `unknown/unavailable`，不暴露异常细节。跨 sender 的记录仅得 `unknown/not-found`。

## 浏览器发送与刷新

1. 当前页面内存中冻结 canonical agent target、conversationId、正文和 summary，生成随机 operationKey。在调用可能产生副作用的 `message.send` **之前**先将键写入当前标签页 `sessionStorage`，再读回并逐字比对。写入、读回或比对失败（包括 quota/privacy mode 异常）时不派发 `message.send`，明确显示“无法保存恢复键，发送未开始”。键不得出现在 URL、日志、analytics 或 localStorage；C1 不提供未经保存键的发送旁路。
2. 同一标签页只允许一个尚未结算的发送意图，避免后一次覆盖待恢复键。`sessionStorage` 只保存操作键；不保存 target、conversationId、正文、summary、token、principal 或 `from/actor/sessionRef`。退出、明确取消或身份切换时清除键。
3. `message.send` 明确返回 durable accepted 时，显示“daemon 已持久接受该操作键”，随后清除键；不能写“对方已收到”。响应丢失或结果 unknown 时保留键，禁止自动补发。
4. 刷新有键时，通过同一注入的 GeneratedClient 调 `message.send.key_lookup`，请求体只有键。`accepted` 显示“该操作键已有持久发送记录；不表示对方收到，也不能证明原文与丢失输入完全一致”，用结果的 conversationId 打开会话并清除键。`unknown` 显示“无法确认该操作是否已持久接受；不会自动重发”，保留键供手动再次查询。无键时只说明无法恢复原操作。
5. 用户重新输入属于新意图、新键。旧键的 unknown 不是拒绝或安全重试许可。同键不同正文不新增摘要/意图权威；key-only 查询不比较或确认原文。

## C1 单目标发送约束

`message.send` 的 HTTP 参数投影在副作用前增加以下约束；现有身份字段和 IPC schema 不变：

```json
{
  "to": {"type": "array", "minItems": 1, "maxItems": 1,
         "items": {"type": "string", "minLength": 1}},
  "message": {"type": "string", "minLength": 1},
  "summary": {"type": "string", "minLength": 1, "pattern": "\\S"},
  "conversationId": {"type": "string", "minLength": 1, "pattern": "\\S"},
  "idempotencyKey": {"type": "string", "minLength": 22, "maxLength": 128,
                     "pattern": "^[A-Za-z0-9_-]+$"}
}
```

`to[0]` 还须经现有 `parse_agent_uri` 验为 canonical agent；多目标及 user/route/host 等非 canonical agent 目标返回 `INVALID_ARGUMENT`，不得到达 daemon 写入。Contract Mock 执行同一约束。IPC 的既有多目标路径和参数校验先后顺序不改，HTTP 收紧不下沉为 legacy 行为变化。

## 身份、home 与代际

daemon 走现有 `_authenticated_message_caller(params, method=..., trusted_origin=...)`，从已验证 principal 派生 sender；不接受浏览器自报。HTTP 使用上述 IdentityBinding、`readiness="inbox-service"`，`from/sessionRef` HTTP 禁止。只读当前 home 的 sender copy；不同 sender、owner、home 的同键不能返回 accepted 或正文。过期身份、旧 generation、被取代 session 在读 DB 前沿现有 `STALE_SESSION`、`SESSION_SUPERSEDED`、`INVALID_SESSION_SOURCE` 拒绝，不能降级为成功。刷新后当前有效 session 的同一 principal 可以查先前键，但不能跨 principal/home。

## 只读查询算法

先按 sender 和**完整操作键前缀**观察所有候选，再判 `intent/lifecycle`；不得在 SQL 里先过滤坏行。复合键为 `operationKey:target:index`，target 本身含 `:`，应从右端拆 index，再按已知键前缀拆 target。示意 SQL：

```sql
SELECT message_id, conversation_id, recipient, idempotency_key,
       payload, intent, lifecycle
  FROM sender_copies
 WHERE sender = :sender
   AND (idempotency_key = :operation_key
        OR substr(idempotency_key, 1, length(:operation_key) + 1)
           = :operation_key || ':')
 LIMIT 2;
```

`substr` 等值比较避免 URL-safe 键里的 `_` 被 SQL `LIKE` 当通配符；若实现改用 `LIKE`，必须真实转义并加负例。建议索引 `sender_copies(sender, idempotency_key)`；索引不改变持久事实。零行=`unknown/not-found`；多行=`unknown/invalid-record`，即使其中有非 request/非 durable 行也不能过滤后宣称唯一。

唯一行还须满足：复合键精确解析且操作键相等，target 是 canonical agent，index 是整数且等于 0，recipient 也是 canonical agent 且与 target 一致；messageId 和 conversationId 非空；payload 为 JSON object 且 `message` 为**非空**字符串；`intent='request'`、`lifecycle='durable_service'`。任一失败=`unknown/invalid-record`；SQLite/OS 读失败=`unknown/unavailable`。不读取 outbox、inbox、投递 receipt，不返回原文。IPC 多目标会留下同键多行，新查询必须 `invalid-record`，不能用 index 0 推断全体成功；未来逐目标状态另立设计。

不新增 TTL/清理；sender copy 随现有 daemon 持久化保留。消息过期不删副本；若未来清理删副本，查询为 `not-found`；数据库不可读为 `unavailable`。二者均不能触发重发。须验证同一 home 的 daemon 重启后返回相同 messageId。

## 可执行验收表

| 场景 | 必须断言 |
|---|---|
| 提交后响应丢失，刷新并查询同键 | 发送前已在 sessionStorage 保存键；刷新只传键；accepted 与原 messageId 相同，实际 send 次数为 1；无正文泄露 |
| 提交前断线、零行、键丢失 | 零行 not-found；无键只提示不能恢复；页面不自动重发 |
| 单行非 request、单行非 durable、有效行加任一无效行 | 均为 invalid-record；同键坏行不能被 SQL 过滤后仍返回 accepted；无正文 |
| 坏 payload/复合键、空正文、index 非 0、recipient 与复合键 target 不一致 | invalid-record，不返回 accepted 或原文 |
| URL-safe 键含 `_`，`a_b` 与 `axb` 等相近键并存 | 只匹配完整键加 `:`，不把 `_` 当通配符，不混淆其他键 |
| DB/OS 读取失败 | unavailable、HTTP 200 unknown，无自动重发或异常细节 |
| 伪造 sender/from/actor，跨 owner/home，旧 generation/session | HTTP forbidden 字段拒绝；非本 sender/home 键为 not-found；失效身份在查库前按会话错误拒绝；绝不 accepted |
| 同键不同正文 | 不返回正文、不宣称原文相同、不触发补发 |
| HTTP 多目标、非 canonical target | HTTP 400/INVALID_ARGUMENT，零 daemon 写入；Contract Mock 同行为，legacy IPC 多目标不变 |
| 旧 `message.send.lookup` | 仍需 text，逐字 mismatch 为 request-mismatch，缺行为 not-found |
| daemon 关闭并以同一 home 重启 | 键查询 accepted、messageId 不变、实际 send 次数为 1 |
| sessionStorage 写入/读回/比对失败 | 显示“无法保存恢复键，发送未开始”，`message.send` 调用数为 0；不得声称发送成功 |
| 发出请求后、响应到达前立即刷新 | 键已存在；只用键调用 key_lookup；`message.send` 调用数仍为 1 |
| 键已保存但 daemon 无记录 | `unknown/not-found`，不自动补发，键留待手动再查 |
| 浏览器存储/同页并发 | 只存键且发送前读回确认；同页最多一个未结算意图；键不入 URL、日志、localStorage |
| Contract Mock 与真实 daemon | 同一页面、adapter、GeneratedClient 和 URL；accepted/unknown 符合契约，unknown 无 fixture 成功兜底 |

## 放行顺序

1. #2423 只改设计文档，merge-forward 后提交新固定 SHA/tree/base 给原安全 reviewer 复审；未 PASS 前方法名/schema 仍为待核文本。
2. 设计 PASS 后，本线完成 daemon 业务实现、MethodSpec/examples 和负例；#2426 当前是独立 WIP，旧焦点测试不等于新契约实现 PASS。
3. 公共 B 与 legacy 错误/字段顺序获独立 PASS；公共线登记 `message.send` 与 `message.send.key_lookup`，生成 OpenAPI/client/catalog pin，核 Contract Mock 一致。
4. 本线通过共用 GeneratedClient 接普通 Chat 页面，真实 daemon 与 Contract Mock 对同一页面分别验收发送、断响应、刷新、unknown、不重复投递。新固定 SHA 独立审核后由统筹合入。

真实外部 IdP/Lark/provider、wheel/native、远端 CI 和完整浏览器验收在设计复审阶段均为 UNKNOWN，不用设计 PASS 替代运行时验收。
