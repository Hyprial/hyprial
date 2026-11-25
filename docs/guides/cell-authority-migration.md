# 迁移说明：adapter 绑定与 allow/deny 策略改由 agent cell 文档持有（I6-4）

适用于：从仍提供 `adapter.pin` / `adapter.pins` / `adapter.unpin` 与
`authority.set` / `authority.get` / `authority.list` 的版本，升级到包含
I6-4（`agent.cell.get` / `agent.cell.update` 公开、pins 与 authority 公开面退役）的版本。
读者是做升级的人，或替人做升级的 agent。

一句话：adapter → agent 的绑定与 harness 的 allow/deny 策略，唯一权威
改为每个 agent 的 **cell 文档**（OrgFS 中的 `("cell", <c.id>)` 文档）。
升级**不会自动迁移** pins 表与 AuthoritySet JSONL；升级后旧数据不再被
读取，也不再生效。本说明给出升级前导出、升级后重建的手工步骤。

本说明只写当前 dev 上已经成立的事。标 UNKNOWN 的是没有在真实环境核实的。

## 1. 退役与替换对照

| 退役（本版本删除） | 替换 |
|---|---|
| `adapter.pin` / `adapter.unpin`（IPC 与 CLI `hyprial adapter pin|unpin`） | `hyprial agent cell apply <agent> --expected-revision N --file CELL.json`（给目标 agent 的 cell 增加/移除 adapter 成员） |
| `adapter.pins`（IPC，含 GUI 读取） | `adapter.list` 的 `agent` 字段（由 cell 路由索引解析）与 `hyprial agent cell show <agent>` |
| `authority.set` / `authority.get` / `authority.list`（IPC 与 CLI `hyprial agent authority …`） | cell 文档每 harness 成员的 `allow` / `deny`（I6-5 才接入应用循环，见第 4 节） |
| identity `pins` 表（`state/agents.sqlite3`）与 pin 命令、removal pin custody | 不再读取；可手工 `DROP TABLE pins`（第 5 节） |
| 旧 desired-state `channelPins` staging 的启动迁移 | 该键被忽略，保存时不再写回（第 7 节） |
| agent 记录与 `agent.list` / `ps` 行里的 `pinnedAdapters` | `hyprial agent cell show <agent>` 与 `adapter.list` |
| `agent.destroy.preview` 的 `pinnedAdapters`、`agent.destroy` 的 `unpinnedAdapters` | `adapterBindings` / `unboundAdapters`（同样来自 cell 文档） |

旧脚本里的退役调用会失败：CLI 报 `No such command`，daemon 答
`METHOD_NOT_FOUND`。

## 2. 升级前：导出旧数据

在**升级之前**（旧版本仍在运行时）执行，并把输出保存到安全位置：

1. 导出 pins：`hyprial adapter list --json`（`agent` 字段即 adapter → agent
   绑定），或对每个 adapter 记录 `adapter.pins` 的内容。
2. 导出 AuthoritySet：`hyprial agent authority list --json`（或读
   `state/authority-set/*.jsonl`），按 target 记录最新 APPLIED 的
   allow/deny 与 version。
3. 记录每个受影响 agent 的名字与 URI（`hyprial agent list --json`）。

## 3. 升级后：用 cell 文档重建

先确认目标 agent 的 cell 现状：

```bash
hyprial agent cell show <agent> --json
```

记下返回的 `revision`（例如 7），写一个完整片段文件 `CELL.json`
（`members` 是全量列表，不是补丁）：

```json
{
  "members": [
    {"ref": "<harness 成员 ref>", "role": "harness"},
    {"ref": "<adapter 名>", "role": "adapter"}
  ],
  "params": {
    "cell.entry": "<harness 成员 ref>",
    "cell.members": {
      "<harness 成员 ref>": {"kind": "<harness 类型，可省略>"},
      "<adapter 名>": {"adapter": "<adapter 名>"}
    }
  }
}
```

应用（CAS：`expectedRevision` 必须等于上一步读到的 revision）：

```bash
hyprial agent cell apply <agent> --expected-revision 7 --file CELL.json
```

- **adapter 绑定**：为每个旧 pin 建一个 `role: "adapter"` 的成员，其
  `cell.members.<ref>.adapter` 指向本机 adapter 配置名。cell 文档没有
  一对一约束：同一 agent 可持多个 adapter 成员；同一 adapter 被两个
  agent 的 cell 声明时是**冲突**，路由 fail-closed（`adapter.list` 的
  `agent` 为 `null`），请先解除其中一处。
- **allow/deny**：在对应 harness 成员的 `cell.members.<ref>` 里写
  `allow` / `deny`（见第 4 节的生效时点）。冲突或 stale
  `expectedRevision` 会返回 `INVALID_ARGUMENT` 并携带
  `expected`/`current`，重读后重试。
- 未迁移的旧 pin **不再隐式生效**；未重建绑定期间，对应 adapter 的入站
  消息没有投递目标（`adapter.list` 的 `agent` 为 `null`）。

## 4. AuthoritySet 的顺序边界（重要）

I6-4 只删除公开的 `authority.*` 写/读面；独立的 `authority_service`
应用循环保留，仅 drain 既有任务，**不接收新任务**。cell 文档里的
`allow` / `deny` 在 **I6-5**（AuthoritySet 应用循环并入成员 reconcile、
独立存储删除）才成为生效策略。因此：

- 升级前已排队未应用的 authority 任务：随 daemon 启动照常 drain 一次；
- 本版本期间修改策略的唯一方式是写 cell 文档，但它 **在 I6-5 之前不
  改变运行中的工具授权**；worker 当前的硬编码默认清单在本版本仍然
  有效（UNKNOWN：未在真实 harness 上逐项核对每类 worker 的现行默认）。
- 不要在本版本依赖「写入 cell 的 allow/deny 已生效」。

## 5. 本地旧数据的处理（手工，无自动迁移）

- `state/agents.sqlite3` 的 `pins` 表：新版本完全不读。确认第 3 节重建
  完成后，可手工 `DROP TABLE pins;`（先备份数据库文件）。
- `state/authority-set/` 的 JSONL：新版本只 drain 不读新任务；drain 完毕
  后可整体归档或删除。
- `desired-state.json` 的 `channelPins`：新版本忽略该键，下次保存时不再
  写回（第 7 节），无需手工处理。
- 升级时仍有未完成的 adapter removal 意图：恢复逻辑只补偿 harness 注册
  与配置文件，不再恢复 pin（pin 已随 pins 表退役，绑定以 cell 文档为准）。
  建议升级前让进行中的 removal 完成或中止后再升级。

## 6. GUI 与 CLI 变化

- GUI「Agent → 平台绑定」页的绑定/解绑：改为 `agent.cell.get` +
  `agent.cell.update`（CAS），冲突时提示重读；不再调用 `adapter.pin(s)`。
- GUI「平台接入」删除确认：显示 `boundAgent`（来自 cell 路由），删除
  只移除本机配置，**保留** Agent 与其 cell 绑定。
- 新 CLI：`hyprial agent cell show <agent>` 与
  `hyprial agent cell apply <agent> --expected-revision N --file CELL.json`。
- Lark 聊天的 `/hyprial agents pin|unpin`：只读显示当前绑定，并指向
  `hyprial agent cell show/apply`；聊天内改绑仍不支持。

## 7. F1b：删除 `channel_pins` 存储（保留旧表，无自动迁移）

- 新版本不再读写 desired-state 的 `channelPins`：文档里遗留的该键被忽略，
  下次保存时不再写回；内容格式错误的旧键也不再阻止加载。
- 新建的 `state/lifecycle-operations.sqlite3` 不再创建 `channel_pins` 表。
  升级前已有的表与其中的行保持原样、不再读取，也不会被代码删除。
  **不要手工 `DROP` 这张表**：回滚到旧版本时它仍要读这张表（见下）。
- 回滚到紧邻的上一版本（仍是 adapter 模型、schema 3）：该版本仍读取
  `channel_pins` 表，读到的是升级时遗留、此后不再更新的旧行；表不存在
  时它会先建空表，读到空集合。回滚前先按旧版本的方式核对或重新固定。
- 经 `hyprial adapter downgrade-state` 回退到 schema 2 的 adapter 模型
  之前版本：该版本加载 desired-state 时直接查询 `channel_pins`，**不会**
  建表；表不存在则启动失败（`no such table: channel_pins`）。以下两种
  数据库没有这张表：由新版本首次从旧 `.v1` JSON 迁移出的数据库，以及
  被手工删表的数据库。前提：用新版本执行 `downgrade-state` 成功后、
  启动旧版本之前，在 daemon 停止时确保表存在（先备份数据库文件）：

  ```sh
  sqlite3 "<HYPRIAL_HOME>/state/lifecycle-operations.sqlite3" "CREATE TABLE IF NOT EXISTS channel_pins (channel TEXT PRIMARY KEY, agent TEXT NOT NULL);"
  ```

- `downgrade-state` 重建的 schema 2 文档不含 `channelPins`：从 `.v1`
  JSON 迁移来的 pin 不会被恢复；已在表里的遗留行保持原样。
- adapter removal 日志（`adapter_removal_intents`）的 `legacyPin` 字段保留，
  新写入的记录恒为 `null`；升级前未完成的 removal 记录照常恢复，其中的
  `legacyPin` 值不再写回任何存储。

## 8. F1c：退役 `channel:` 地址写法（无改写代码）

`channel:<owner>:<machine>:<name>` 与三段 `channel:lark:<adapter>` 是
`adapter:` 地址的旧写法。本版本起：

- **owner 绑定（影响 `user:` 会话 id）**：用户档案的 squire 端点仍接受
  `channel:` 写法（随 squire 档案一并删除），但会话层不再读它：仍用
  `channel:` 端点的 owner 绑定不再生成 adapter 线程，`user:<owner>` 会话从
  绑定的 owner 线程回到普通 user 线程，即**这个 `user:` 会话的 id 改变**。
  需要绑定的，把端点重设为同段的 `adapter:` 写法。
- 八个通用 adapter（discord、imessage、mail、matrix、qq、slack、telegram、
  whatsapp）入站 `ChatContext.channel_id`（回执里的 `channelId`）改写为
  `adapter:<kind>:<instance>:<chat>`；Lark 早已只写 `adapter:lark:<name>`。
  升级前写入的回执保留原值，不改写。
- 除用户档案的 squire 端点外，所有读取点不再把 `channel:` 当作 `adapter:`：
  - 会话标识：`channel:` 端点按原文参与 `dc.` 默认会话 id，因此与它的
    `adapter:` 写法得到**不同的** id；`channel:` 端点不再得到 adapter 线程
    或 `p.` 配对键。已经带有存储线程戳的旧行保留原来的会话；靠推导归属的
    旧行不再与新的 `adapter:` 消息归入同一会话。**已写入会话记录的旧
    `channel:` 条目留在旧 id 下，不迁移、不合并**；升级时尚未入记录（或经
    读取路径重新归属）的、无线程戳的 `channel:` 行没有任何会话归属，不出现在
    `conversations.list` 中。
  - 回复：对发件人为 `channel:`（含 `channel:lark:<adapter>`）的旧待处理
    消息执行 `message.reply` 会以 `MESSAGE_REPLY_UNAVAILABLE` 拒绝，原消息
    不确认、保持待处理，不写入 outbox；收件人为 `channel:` 的发送按未知地址拒绝
    （`UNSUPPORTED_TARGET`），未指定生命周期时按 online-only 处理。
  - outbox：收件人为 `channel:lark:<adapter>` 的旧排队行成为不可投递行，
    `hyprial message outbox prune` 会把它们移入死信（可先加 `--dry-run` 查看）。
  - 展示：没有来源记录的 `channel:` 发件人的 `origin.via` 显示为
    `daemon`（原为 `adapter`），告警受众判为 `agent`（原为 `human`）。
- 升级前若要保留关联：先处理完待回复的 Lark 旧消息、清空发往 `channel:`
  的 outbox；脚本与配置里写死的 `channel:` 地址改为同段的 `adapter:`。

## 9. I6-5a：allow/deny 由成员 reconcile 生效

从包含 I6-5a 的版本起，第 4 节的顺序边界结束：

- 策略只读 cell 文档：harness 成员的 `cell.members.<ref>.allow` / `deny`
  （各为该 harness 自己的规则字符串列表，原样传给 launch）。不写这两个键即
  「未声明策略」，代码不再补默认值。`claude` 成员两个键都没有时，agent cell
  registrar 把 `managed-worker` 模板规则写入文档一次（成员 harness 已知时，
  以及 `hyprial agent start` 启动前）；已声明任一键的成员不会被改写，其他
  harness 的成员不写入。
- 每一次启动（`hyprial agent start`、daemon 重启后的恢复、崩溃重启）都只从
  cell 文档取策略：先用 reconcile 已发布的策略，否则读本机 cell 缓存中的文档
  （只读，不访问 OrgFS）。文档没有可用策略时 worker 只带 harness-bridge
  工具，并记录 `cell.member_policy_unavailable`。
- 修订只改变策略时，reconcile 对该成员执行 `cell.member.policy.apply`：停止、
  按新策略启动、校验 worker ready 帧回执的 digest 与版本（cell revision）。
  成功后 `policyDigest` 即新策略；失败时重启上一次已验证的策略，成员继续以旧
  策略运行但不再接收新工作，cell 为 `degraded`，文档保持不变。再提交一个修订
  （改正或回退策略）即可恢复。
- `allow` 与 `deny` 类型错误、重复或重叠：整个修订被拒绝（结构问题
  `members-policy-type` / `members-policy-overlap`），继续沿用上一个有效修订。
- 当前只有 `claude` 有策略映射；其他 harness 成员声明策略时该成员失败、
  cell 为 `degraded`，不会静默忽略。
- 独立 authority 应用循环不再随 daemon 启动；`HYPRIAL_AUTHORITY_SUPERVISOR`、
  `HYPRIAL_AUTHORITY_STORE`、`HYPRIAL_AUTHORITY_AUDIT` 不再起作用。
  `state/authority-set/` 与 `<daemon state>/authority/` 下的 JSONL 不再被读取，
  可在确认第 3 节重建完成后手工归档或删除。升级前仍未应用的 authority 任务
  不会再被执行，请改写进 cell 文档。
- 升级后第一次启动 daemon 时，若 cell runtime 尚未组合，恢复启动的 `claude`
  worker 读到的是尚未写入模板规则的文档，只带 harness-bridge 工具、不能执行
  `hyprial` CLI。对该 agent 执行一次 `hyprial agent stop` 再
  `hyprial agent start`（启动前会写入模板规则），或用
  `hyprial agent cell apply` 显式写入 `allow` / `deny`。只写 `deny` 时
  `allowedTools` 为空：请同时声明需要的 `allow`（含 `mcp__harness-bridge__*`）。


## 10. I6-5b：独立 AuthoritySet 存储与审计删除

- `daemon/impl/authority_set/`（任务模型、JSONL 存储、apply 循环、JSONL 审计）
  与 `daemon/impl/authority_service/` 已删除；代码不再读写
  `state/authority-set/` 与 `<daemon state>/authority/` 下的任何文件，可按第 9
  节手工归档或删除。
- 策略应用结果改为 cell 可观测事件，写入 daemon 日志（domain `cells`），不再有
  单独的 JSONL 审计文件：
  - `cell.member_policy_apply_started`：策略变更开始应用；
  - `cell.member_policy_applied`：worker 确认新策略（`action=apply`），或首次以
    策略启动并确认（`action=ensure`）；
  - `cell.member_policy_apply_failed`：未确认；`rolledBack` 表示是否已重启上一次
    已验证的策略，`rollbackCode` 为回滚失败原因。

  每条事件带 `cell`、`agent`、`member`、`harness`、`revision`（即策略版本）、
  `policyDigest` 与 `previousPolicyDigest`。reconcile 接管一个已确认该策略
  的运行中 harness 时记 `cell.member_policy_applied`（`action=adopt`）。
- 一个 agent 只有一个 harness：`hyprial agent start` 启动的 `<harness>:<agent 名>`。
  策略或配置变化时 reconcile 停止并重启这同一个 harness（续用已记录的会话），
  不会再以 agent URI 另起第二个。会话无法续用时什么都不停止：旧策略继续运行，
  cell 为 `degraded`。文档里的 `kind` 与正在运行的 harness 不同会在停止任何东西之前被拒绝，
  agent 继续运行、cell 为 `degraded`，直到文档改回；切换请用
  `hyprial agent start --harness`。新配置失败（策略未确认、`model` 变更起不来）时，
  reconcile 以失败前运行的配置与策略重新拉起同一个 harness，成员不再接新工作、cell 为
  `degraded`。重启时不删除 desired-state 行，daemon 在停与启之间被杀，下次启动会恢复该
  agent；重拉也失败时 agent 停下、成员为 `failed`，直到下次启动、改变其配置的修订或
  `hyprial agent start`（KP-131）。这样保留下来的成员在其配置未变的后续修订中仍为
  `degraded`，直到它确认文档规则。这次重拉不钉住任何策略，之后的
  `hyprial agent stop` + `hyprial agent start` 会重新读取文档规则，reconcile 检查该次启动的确认后才恢复接收工作。这偏离了裁决 §12.1.3「审计保留」：
  owner 批准（Q0-e）以 cell 可观测事件取代独立审计存储。
