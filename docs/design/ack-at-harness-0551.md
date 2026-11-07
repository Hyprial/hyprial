# 0.5.5.1 第 6 项：确认点从 model 移到 harness（取消 agent 层逐条 ack）

状态：设计，待实施。归属：0.5.5.1，与 inbox 性能修复同一集成分支 `intg/daemon-inbox-perf`（#1892）。
来源：Allen 2026-10-24（飞书原话）——「我认为不应该在 agent（harness）层添加 ack，收到消息并加入 context 中供 model 处理的行为，实际应该由 harness 保证，而不是要求 hyprial 框架去保证，在网络层面 ack 就够了。」以及「这个应该写入 0.5.5.1，和 inbox-fix 一起」。

## 1. 现状（dev 5dc2d1c95，已核对代码）

一条消息有三种确认：

| 层 | 谁做 | 含义 | 位置 |
|---|---|---|---|
| 网络层 | 接收方 daemon 自动 | 消息已在同一事务里写进接收方 durable inbox；发送方 outbox 记为 `fetched / ACK_RECEIVED` | `inbox/service/core.py:715-730`，`inbox/links/pull.py:90` |
| 取用 | 读取方（harness 或 CLI） | `fetch_pending` / `batch_snapshot` 是消费式读：标记 fetched，但不结算 | `inbox/authority/facade.py:406,477`，`service/projection.py:397,738` |
| 结算 | **model 主动调用** `harness_ack` / `harness_reply`（CLI：`message ack` / `message reply`） | 从 pending 移除；未结算的消息一直留在 inbox | `facade.py:236`，`service/delivery/retry.py:65`，`turn_delivery/channel/worker_channel.py:32-37` |

「N 条请求未回复」提醒只统计 pending 中 `intent == "request"` 的消息（`application/messaging/inbox_surface/reply_reminders.py:42-110`）。

## 2. 问题

- **分层错误**：「消息已交给处理方」是 harness 知道的事实，现在却要 model 替它记账。
- **加重 inbox 过载**：每条 ack 都是一条进入 inbox actor 单一 mailbox 的命令。生产 0.5.5 在启动后约 10 分钟就出现 ack 超时、排队 39（另见 #1892 第 1 项）。
- **失败放大**：model 漏 ack → 消息留在 pending → 提醒、看门狗 → 更多消息、更多命令。
- **token 成本**：每条消息都要多一次工具调用。

## 3. 方案

1. **harness 注入成功即自动结算（durable）**。connector 代码在消息被放进 model 的 context（turn input）之后，向 daemon 提交结算；model 不再调用 ack。
   - 结算只在注入成功之后提交：注入失败或 harness 离线时不结算，daemon 照常重投。
   - 「注入成功」的判定由各 connector 定义，见 §4。
2. **request 保留 reply**。reply 是业务上的回答，不是投递确认。request 注入后，投递层面同样按 1 结算；「未回复」改为看 request 是否有 reply，不再看它是否还在 pending。需要一个独立于 pending 的「request 已回复」读数，供提醒使用。
3. **batch 整批结算**。`message batch`（消费式快照）在 harness 注入整批之后，由 harness 按快照游标一次结算，不再逐条。
4. **注入之后、model 处理之前会话崩溃**：消息在 harness 的会话记录里，恢复时 model 能看到。这一段按 Allen 的裁定归 harness 负责，hyprial 不再兜底。
5. **兼容**：0.5.5.1 期间保留 `message ack` / `harness_ack` 作为可选调用（幂等、对已结算消息返回 ok），不再要求；文档和 skill 改为「无需 ack」。

## 4. 需要按 connector 确认的点（实施前逐个核对，不凭推断）

- claude channel：消息经 MCP channel 推送进会话时，以什么为「已注入」？
- codex（app-server / TUI）：turn input 提交成功的回执在哪里？
- pi（SDK bridge）：同上。
- 隔离 home 的 worker（python worker channel）：现在由谁结算？
- 未注入就被 harness 拒收（如会话正忙）的消息：保持 pending，重投节奏沿用现有 retry。

## 5. 验收（每格都必须能变红）

1. 负对照（改前红）：一条非 request 消息注入后，model 不调用 ack，消息仍留在 pending，并触发提醒或看门狗。
2. 改后：注入后自动结算；inbox 不再有该消息；没有提醒。
3. harness 离线或注入失败：消息保持 pending，并被重投；不能被误结算。
4. request：注入后投递已结算，但在没有 reply 时仍出现「未回复」提醒；reply 之后提醒消失。
5. batch：整批注入后一次结算；结算前崩溃 → 整批重投。
6. 负载：同负载、同时长，改前改后的 inbox 命令数和 ack 超时数（`daemon status` 的 ipcStats），原始数据落 docs/notes。

## 6. 契约与文档

- 改变的是 agent 的操作方式和投递语义：需要 `Contract-Impact`（contract/ 的投递与 MCP 工具说明）以及 `Skill-Impact: updated`（hyprial-ops 等 skill 里的「每条 ack」说明）。
- 生产 0.5.5 观测到的另外两项，交由 #1892 统一排期：PAC `WorkflowTick` 每次约 1.49 秒（占 CPU 约 27%，比 0.5.4 慢）；`hyprial upgrade` 的 30 秒就绪等待导致误报 `UPGRADE_RESTART_FAILED`（应以新 pid + epoch 变化判成功）。
