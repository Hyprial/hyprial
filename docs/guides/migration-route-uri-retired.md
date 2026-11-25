# 迁移：`route:<adapter>:<route>` 地址退役（I4，2026-11-24）

`route:<adapter>:<route>` 地址族分两步退役（owner 批准 Q0-a / Q0-d，2026-11-23）：

- **I4a**：daemon 内部不再把 `route:` 当作端点或请求方读取（第 2 节）。
- **I4b**：删除 `route:` 发送入口、`targetKind: channel_route`、四个线路错误码
  `ROUTE_ADAPTER_UNCONFIGURED`、`ROUTE_SEND_FAILED`、`ROUTE_RESOURCES_UNSUPPORTED`、
  `ROUTE_RESOURCE_REPLY_UNSUPPORTED`，附件改走 cell 推送；Lark 用户授权通知的 `notifyRoute` 改为
  adapter 自己的路由名（第 3–6 节）。

保留不变：`adapter.routes.*` 请求与其 `ROUTE_NOT_FOUND`、`ROUTE_EXISTS`、`ROUTE_IN_USE`；
session-route-lease 的 `ROUTE_CLOSED_BEFORE_COMPLETION`、`ROUTE_IO_FAILED`、`ROUTE_LEASE_STALE`；
routine 的 `route:self` / `route:worker:<name>` 标签（它们是 routine 路由标签，不是地址族）。

没有迁移代码；按下面的说明改调用方与配置即可。

## 1. 给飞书里的人发消息：改发 `user:<owner>`

agent 不再点名 `route:<adapter>:<route>`。改为：

```sh
hyprial message send --from <自身 URI> --to user:<owner> --summary "<≤512 字节>" "<正文>"
```

由**发送方 agent 所在 cell** 的 `on: "user"` 连接决定推到哪个 chat：连接上的 `route` 是 adapter
自己的路由名，不写则用该 adapter 的默认路由；cell 里没有任何成员持有 `on: "user"` 连接时按 cell 的默认推送规则处理；连接由同一 cell 的其它成员
持有时，本成员的发送没有推送目标（`TARGET_UNRESOLVED`），不会退回默认规则
（见 [按 agent 绑定推送](migration-im-push-by-agent-binding.md)）。想换 chat，改 cell 文档
（`hyprial agent cell show|apply`），不要在发送命令里写地址。

**文件和图片**（`--file` / `--image`）也发给 `user:<owner>`，见第 4 节。

## 2. 历史会话与告警

- 与 `route:` 对端之间的行不再推导会话键（`direct_key`）或会话对键（`p.` key）。I4a 之前
  已带归属戳写入的行仍按存储的戳归档、可读，但会话列表里这类会话的 `kind` 为空（`null`，原为
  `"adapter"`）；没有戳的旧行不再出现在会话列表里。不提供重写。
- 失败/无进展告警：请求方是 `route:` 地址时不再视为"面向人"的请求方，也不再据此回推到
  Lark chat（`route:` 从来不是请求的发起方，正常流量不受影响）。

## 3. `route:` 发送入口已删除（I4b）

- `hyprial message send --to route:<adapter>:<route>`、MCP `harness_send`、Pi `harness_send`
  发给 `route:` 地址，一律在入队和推送之前以 `UNSUPPORTED_TARGET` 拒绝（`data.target` 是该地址）。
  改发 `user:<owner>`（第 1 节）。
- `targets` 请求、`hyprial message targets`、MCP / Pi `harness_targets` 不再列出
  `targetKind: channel_route` 的行，`kind: channel_route` 以 `INVALID_ARGUMENT` 拒绝。
  查看配置的路由用 `hyprial adapter route list <adapter>`。
- `message.send` 的结果不再有 `routeDelivery`、`chatId`、`fanoutOf` 字段。
- `hyprial message probe-delivery-paths` 只探 agent 与 user 两族，`--route` 选项已删除；
  `hyprial doctor` 的 `delivery-paths` 检查只看这两族，旧探针文件里的 `route` 项被忽略。
- 出站队列里收件人是 `route:` 的旧行视为不可投递（`route:` 发送从不入队，正常不会有这种行）。
- `channel` 能力授权：`message.send` 不再有它的入口，这类授权不再影响任何发送；能力本身随后退役，见[channel 能力退役](migration-channel-capability-retired.md)。

## 4. 附件改走 cell 推送（I4b）

```sh
hyprial message send --from <自身 URI> --to user:<owner> --summary "<≤512 字节>" --file <路径> "<说明>"
```

- 附件随文字副本推到同一批 chat（第 1 节的规则），每个 chat 先发文字、再按顺序发附件；结果里
  `pushed: true` 且 `resourceDeliveries[]` 列出已发出的附件。
- 只有本机 owner 的 `user:<owner>` 能带附件。发给 agent 或别的 owner 带附件，以
  `UNSUPPORTED_TARGET`（原 `ROUTE_RESOURCES_UNSUPPORTED`）拒绝；带 `replyTo` 的附件以
  `INVALID_ARGUMENT`（原 `ROUTE_RESOURCE_REPLY_UNSUPPORTED`）拒绝。两者都在任何副作用之前。
- ⚠️ 推送只尝试一次。`pushed: false` 时文字和附件都没有发出，消息本身照常入队；看 `pushCode` 与日志
  `send.user_push`。文字已发出而某个附件失败时，结果是 `pushed: true` 带该附件的 `pushCode`，
  `resourceDeliveries[]` 只列失败之前已发出的附件，同一 chat 余下的附件不再尝试（日志 reason
  `resource-partial`）。以同一个 `idempotencyKey` 重试 `message.send` 时每条推送沿用原来的 key，由飞书按 key 去重；
  CLI 的 `hyprial message send` 每次换新 key，重发会让文字再到一次。push-only 路径同样
  答 accepted 并带 `pushCode`，不再报“没有推送任何副本”。

## 5. 线路错误码（I4b）

删除（注册表、冻结表与 `message.send` 声明中均已删除，无兼容读取）：

| 旧码 | 现在 |
|---|---|
| `ROUTE_ADAPTER_UNCONFIGURED` | 推送目标的 adapter 未配置：每目标 `TARGET_UNRESOLVED`（日志 `reason: adapter-unconfigured`） |
| `ROUTE_SEND_FAILED` | 平台拒绝：每目标 `USER_DELIVERY_FAILED` |
| `ROUTE_RESOURCES_UNSUPPORTED` | `UNSUPPORTED_TARGET` |
| `ROUTE_RESOURCE_REPLY_UNSUPPORTED` | `INVALID_ARGUMENT` |

`message.send` 也不再声明只属于 `route:` 入口的 `ROUTE_NOT_CONFIGURED`、`ROUTE_FANOUT_MEMBER_INVALID`、
`ROUTE_APP_SCOPE_REQUIRED`、`ROUTE_APP_NOT_IN_CHAT`、`ROUTE_APP_BOT_ABILITY_DISABLED`、
`ROUTE_OPERATOR_NOT_IN_CHAT`。保留不变的码见文首。

## 6. Lark 用户授权通知：`notifyRoute` 是路由名（I4b）

- `hyprial adapter user-auth check --notify-route <路由名>`、环境变量
  `HYPRIAL_LARK_REAUTH_NOTIFY_ROUTE` 与状态文件里记住的 `notifyRoute` 现在都是 adapter 自己的路由名
  （`hyprial adapter route list <adapter>` 里的 name），例如 `dm`。
- 由 `--notify-route` 或环境变量给出的旧值 `route:<adapter>:<route>` 在调用 lark-cli 之前以
  `INVALID_ARGUMENT` 拒绝，提示指向本文。
- 状态文件里记住的旧值不阻断检查：检测、lark-cli 的惰性刷新与授权链接照常进行，只是不发通知，
  结果（含自动更新定时器的检查记录，字段 `notifyError`）带 `error: notify-route-retired`，CLI 另有一条警告。
  记住的旧值不会被自动改写。改法：用新值重跑一次 `check`（`--notify-route dm`），凭据健康时也会
  覆盖记住的旧值；或设置环境变量。
- `hyprial adapter user-auth complete` 遇到记住的旧值时照常完成授权，但不发恢复通知，结果带
  `error: notify-route-retired`。
- 通知经新的 daemon 请求 `adapter.routes.send`（`{route, message, sender}`，仅本机 operator；`sender` 用于 `转述自` 前缀，按 `message.send` 的 `onBehalfOf` 同样解析，且必须是本机已注册的 agent，未注册的名字或完整 `agent:` URI 都答 `SENDER_UNRESOLVED`；每次发出与平台拒绝都以 `adapter.routes.send` 记入 daemon 日志，带 adapter、路由、发送方与 `larkCode`）发出：
  daemon 找出唯一配置了该路由名的 adapter，带 `转述自 <发送方>：` 前缀发出。没有 adapter 有这个
  路由名答 `ROUTE_NOT_FOUND`；多个 adapter 都有答 `INVALID_ARGUMENT`（改名让它唯一）；平台拒绝答
  `USER_DELIVERY_FAILED`；结果未知答 `SUBMIT_OUTCOME_UNKNOWN`。
