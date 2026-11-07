# WP3 实施规格：summary 列、send/reply 必填、pending 只出摘要、read_body、settle_hint

- 设计依据：[design-send-links-2026-10-14.md](../design-send-links-2026-10-14.md) §4.5、§7、§8.1、§9
- 缝：`src/hyprial/daemon/impl/links/ports.py`（`SUMMARY_MAX_BYTES`、`IPC_MESSAGE_READ_BODY`、`IPC_MESSAGE_SETTLE_HINT`、`SettleHint`、`LinkGate`，名字不改）
- 基线：集成分支 `refactor/org-structure-constrain` @ `177929d26`（WP0）
- 本包只改 `src/` 与 `tests/`；`contract/` 由 WP8 同步（见 §6）

## 0. 范围与分期

WP3 分两期提交，各开一个 PR 到集成分支。

| 期 | 分支 | 内容 | 合并前提 |
| --- | --- | --- | --- |
| A（增量） | `wp3/send-links` | 三表加 `summary` 列与迁移；summary 端到端持久化与上线；send/reply **有 summary 就按规则校验**（空、超长、非 UTF-8 均 `SUMMARY_REQUIRED`）；pending.list **新增** `summary`、`bodyBytes`、`kind`（保留 `message`）；新 IPC `message.read_body`、`message.settle_hint`；daemon 内部与 adapter 调用方全部带 summary | WP0 |
| B（硬切） | `wp3/send-links-cut` | send/reply **缺 summary** 即 `SUMMARY_REQUIRED`；pending.list **去掉** `message`；改写依赖旧形状的现有测试 | WP3-A、WP5、WP6 已合并 |

为什么分两期（实测，非推测）：在临时副本里只加两行「缺 summary 即拒」和删掉 pending 行的 `message`，跑 `tests/daemon tests/shell tests/kernel tests/integration tests/tooling/architecture`，扣除副本环境本身的失败后，新增失败 277 条：缺 summary 导致 272 条（41 个文件），删 `message` 导致 6 条。其中经 CLI `hyprial send`、MCP `harness_send`/`harness_reply`、codex/pi 载体的失败，修复代码属于 WP5/WP6，WP3 一个 PR 内无法变绿。A 期让 WP5/WP6 有可编码的 IPC 形状，B 期在它们之后翻转。这只是集成分支内的合并顺序，发布仍是一次硬切（R14），不是运行时影子模式。

明细（B 期要改的现有测试，`文件(条数)`）：`squire/addressing/routing/delivery/test_agent_bot_round2.py(28)`、`kernel/actors/identity/test_resolution.py(22)`、`squire/.../test_agent_bot_review.py(19)`、`routing/test_route_outbound.py(14)`、`squire/.../delivery/test_delivery.py(13)`、`integration/actor_preservation/delivery/channel/test_reply_settlement.py(11)`、`squire/.../test_agent_bot_dm.py(11)`、`adapters/lark/outbound/test_lark_submission_outcome_unknown.py(11)`、`application/transport/channel/delivery/test_delivery.py(10)`、`tooling/architecture/identity/test_sender_identity_gate.py(9)`、`identity/aliases/test_alias_input_surfaces.py(9)`、`delivery/message/test_authenticated_message_sender.py(8)`、`kernel/actors/identity/test_mailboxes.py(7)`、`integration/identity/contracts/test_identity_contract.py(7)`、`dispatch/without/test_dispatch_without_pac.py(7)`、`application/ports/test_daemon_capability_wiring.py(7)`，以及 25 个各 1 至 6 条的文件（`test_adapter_contracts`、`test_visibility_sender_canonical`、`test_target_kind_honesty`、`test_proxy_forward_attribution`、`test_route_attribution_targets`、`test_dispatch_gate`、`test_inbound_origin`、`test_lark_unknown_review_controls`、`test_agent_address_normalization`、`test_forward_as_actor`、`test_send_gate`、`test_agent_send_idempotency`、`test_interactive_session_fetch_contract`、`test_mcp_daemon_alignment`、`test_daemon_visibility_wiring`、`test_wire_delivery`、`test_worker_relay`、`test_local_first_self_node_delivery`、`test_sender_identity`、`test_lark_pending_notice_claim`、`test_receiver_guidance`、`test_pi_headless_worker_fetch_e2e`、`test_mcp_channel_owner_migration_e2e`、`channel/routing/test_routing`、`test_lark_daemon_idempotency`）。删 `message` 的 6 条：`channel/routing/test_routing.py::test_system_notice_is_returned_once_without_ack_or_fifo_settlement`、`channel/sessions/test_sessions.py::test_channel_session_registration_and_pending_read_are_daemon_authoritative`、`delivery/origin/test_inbound_origin.py` 两条、`integration/actor_preservation/delivery/channel/test_registration.py` 两条。B 期实施时以当时重跑的结果为准，上表只给量级。

不在 WP3 内（有意留下 `summary = NULL` 的路径，见 §7）：PAC 的 `InboxIo.deliver` 原始投递、`runtime/forwards.py` 的 agent-sdk 自动回复（WP5 删除）、载体与 MCP/CLI 的 send/reply 入参（WP5/WP6）。

## 1. 改哪些文件

### 1.1 A 期，产品代码

| # | 文件 | 改动要点 | 行数（现→后，估） |
| --- | --- | --- | --- |
| 1 | `src/hyprial/daemon/impl/inbox/contracts/summary.py`（新建） | 纯函数，无 I/O：`check_summary(value, *, required: bool) -> str \| None`（规则见 §2.1，违规抛 `DaemonRequestError(SUMMARY_REQUIRED, …, data)`）；`summary_from_text(text) -> str`（人写原文转 summary 的确定性规则，§2.2）；`body_text(message) -> str` 与 `body_bytes(message) -> int`（§2.3，pending.list 与 read_body 共用，保证两处字节数一致）。从 `hyprial.daemon.impl.links.ports` 取 `SUMMARY_MAX_BYTES`，不自定义常数 | 0→约 100 |
| 2 | `src/hyprial/daemon/impl/inbox/service/core.py` | `outbox`、`inbox`、`custody` 三个 `CREATE TABLE` 各加 `summary TEXT`（不要在列清单里写注释，见文件内既有说明）；迁移：沿用 `message_expires_at_ms` 的写法，`for table in ("outbox","inbox","custody")` 查 `PRAGMA table_info`，缺列则 `ALTER TABLE {table} ADD COLUMN summary TEXT`，**不回填**，旧行为 NULL；`_insert_outbox` 写入 `message.summary` | 660→约 672 |
| 3 | `src/hyprial/daemon/impl/inbox/service/delivery/receive.py` | inbox 的 `INSERT` 写 `summary` | 217→约 219 |
| 4 | `src/hyprial/daemon/impl/inbox/service/delivery/retry.py` | custody 的 `INSERT` 写 `summary`；`_row_message` 读 `summary`，用 `"summary" in row.keys()` 守卫（system_notices、progress_events 表没有该列也走这个函数） | 837→约 845 |
| 5 | `src/hyprial/daemon/impl/inbox/authority/read.py` | `InboxReadProjection._message` 同样读 `summary`（守卫同上） | 669→约 674 |
| 6 | `src/hyprial/daemon/impl/inbox/service/projection.py` | `fetch_pending(recipient, *, now_ms=None, message_id=None)`：给定 `message_id` 时 SELECT 加 `AND message_id = ?`，其余（`fetched_at_ms = COALESCE(...)`、`retire_outbox_receipt`）不变。这就是「单条拉取」，不新增 inbox 变更操作名 | 412→约 418 |
| 7 | `src/hyprial/daemon/impl/inbox/contracts/ports.py` | `FetchPendingCommand` 加 `message_id: str \| None = None`（追加在末尾，带默认值） | 385→386 |
| 8 | `src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py` | `FetchPendingCommand` 分支把 `command.message_id` 透传给 `fetch_pending` | 538→539 |
| 9 | `src/hyprial/daemon/impl/inbox/authority/facade.py` | `fetch_pending(recipient, *, now_ms=None, message_id=None)` 透传 | 624→约 627 |
| 10 | `src/hyprial/daemon/impl/inbox/actor/custody/submit.py` | `_submission_command_digest` 在 `message.summary is not None` 时把 `"summary"` 加进摘要对象；为 None 时对象与今天逐字节相同，已有回执的 digest 不变 | 296→约 299 |
| 11 | `src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py` | `_ipc_message_send`：`_authenticated_message_caller` 返回后**立即** `summary = check_summary(params.get("summary"), required=False)`，早于 `_queue_agent_activity`、别名解析、会话登记与任何投递；agent 目标的 `InboxMessage(..., summary=summary)`；user、route 目标只校验不携带。`_ipc_message_reply`：同一位置同一调用；回复的 `InboxMessage(..., summary=summary)`。不加别的逻辑，send.py 不再变长 | 727→约 733 |
| 12 | `src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py` | pending.list 每行加 `kind`、`summary`、`bodyBytes`（A 期保留 `message`）；新增 `_ipc_message_read_body`、`_ipc_message_settle_hint` 两个方法（§2.5、§2.6）。放这里而不新建文件：两者都是「消费者读」，与 pending.list、ack 同一身份规则；且 inbox 权限门的规范身份 `(queries.py, DaemonApplication.handle, fetch_pending)` 已在清单里，不必扩清单 | 511→约 630 |
| 13 | `src/hyprial/daemon/impl/application/messaging/visibility.py` | `_authenticated_message_caller` 里两处重复的「消费者方法」集合抽成模块级 `_CONSUMER_METHODS` frozenset，加入 `IPC_MESSAGE_READ_BODY`、`IPC_MESSAGE_SETTLE_HINT`。这两个方法因此和 `message.ack` 一样按 `_message_consumer_actor` 解析身份、走同一 session fence、同一 `SENDER_NOT_AUTHENTICATED` 规则 | 701→约 700 |
| 14 | `src/hyprial/daemon/impl/ipc/router.py` | `from hyprial.daemon.impl.links.ports import IPC_MESSAGE_READ_BODY, IPC_MESSAGE_SETTLE_HINT`；`_IPC_STATS_METHODS` 加这两个常量；`handle()` 在 `message.ack` 分支后加两条 `if method == IPC_MESSAGE_…:`，签名与 `_ipc_message_ack` 相同（带 `_trusted_message_origin`） | 850→约 858 |
| 15 | `src/hyprial/daemon/impl/application/__init__.py` | `DaemonApplication` 声明 `_link_gate: LinkGate \| None = None`（只声明，WP2 在 composition 赋值） | 936→约 938 |
| 16 | `src/hyprial/kernel/impl/contracts/channels/worker_channel.py` | `METHOD_PARAMS`：`message.send` 与 `message.reply` 加 `"summary"`；新增 `"message.read_body": frozenset({"messageId"})`、`"message.settle_hint": frozenset({"messageId"})`。kernel 不能 import daemon，此处用字面量，由测试钉住与 ports 常量相等 | 128→130 |
| 17 | `src/hyprial/kernel/impl/contracts/daemon/ipc_errors.py` | 新码 `MESSAGE_BODY_UNAVAILABLE`（read_body 找不到调用者可读的行），加入 `ALL_CODES` | 424→约 429 |
| 18 | `src/hyprial/daemon/impl/application/messaging/delivery/sends.py` | user-proxy 转递（约 298 行处 `params`）加 `"summary": summary_from_text(params["message"])`（在拼好「转述自…」之后取）；forward 转递（约 430 行处）加 `"summary": original.summary if original.summary is not None else summary_from_text(text)` | 757→约 761 |
| 19 | `src/hyprial/daemon/impl/adapters/lark/worker/daemon_link.py` | `_Harness.send_request` 的 `message.send` 参数加 `"summary": summary_from_text(request.text)`。Lark 入站是人写的，没有 summary，由 adapter 作为代发方生成 | 378→约 380 |
| 20 | `src/hyprial/daemon/interface.py` | 导出 `summary_from_text`、`SUMMARY_MAX_BYTES` 给 shell 用（shell 不能 import daemon impl） | 836→约 838 |
| 21 | `src/hyprial/shell/impl/cli/commands/adapter/lark_auth.py` | `message.send` 加固定 summary，例如 `"Lark 用户授权已过期，请按正文完成交接"`（≤512 字节） | +1 |
| 22 | `src/hyprial/shell/impl/cli/commands/workflow/control.py` | `_WorkflowNotificationSender.send` 加 `"summary": summary_from_text(text)`（经 `hyprial.daemon` 导入） | +2 |
| 23 | `src/hyprial/shell/impl/cli/commands/fs/commands.py` | orgfs 邀请的 `message.send` 加固定 summary，例如 `f"orgfs 邀请：{name}"`；名字过长时用 `summary_from_text` | +2 |

### 1.2 A 期，测试支撑（非产品）

| # | 文件 | 改动要点 |
| --- | --- | --- |
| 24 | `tests/support/gates/authority_migration.py` | `INBOX_CROSSWALK` 加一条：`('…/inbox_surface/queries.py', '_MessageQueriesMixin._ipc_message_read_body', 'fetch_pending')` → `('…/inbox_surface/queries.py', 'DaemonApplication.handle', 'fetch_pending')`。`tests/inbox_authority_exemptions.json` **不改**（shrinking-only 不破） |
| 25 | `tests/daemon/ipc/error/test_ipc_error_code_registry.py` | `FROZEN_CODES` 加 `MESSAGE_BODY_UNAVAILABLE`；同时补上 WP0 漏加的 6 个码（`SUMMARY_REQUIRED`、`LINK_PEER_FULL`、`LINK_SELF_FULL`、`LINK_WINDOW_FULL`、`LINK_UNKNOWN`、`LINK_RECLAIMED`）——基线上 `test_registry_membership_is_frozen` 已因此失败（见 §7.1） |
| 26 | `tests/daemon/application/transport/channel/support.py`、`tests/integration/actor_preservation/delivery/channel/support.py` | 两处 `FakeInbox.fetch_pending(self, actor)` 改为接受 `*, now_ms=None, message_id=None` 并按 `message_id` 过滤 |

### 1.3 B 期

| 文件 | 改动要点 |
| --- | --- |
| `inbox_surface/send.py` | 两处 `required=False` 改为 `required=True` |
| `inbox_surface/queries.py` | pending.list 行去掉 `message` 键 |
| §0 列出的现有测试 | 直接调 `handle("message.send"/"message.reply")` 的补 `"summary"`；断言行内 `message` 的改为断言 `summary`/`bodyBytes`，需要正文的改调 `message.read_body`。经 CLI、MCP、载体的用例此时已由 WP5/WP6 改好，B 期不碰它们的产品代码 |

### 1.4 结构硬限制核对

| 目录 | 直接文件数 现→后 | 说明 |
| --- | --- | --- |
| `daemon/impl/inbox/contracts/` | 3→4 | 新增 `summary.py` |
| `daemon/impl/application/messaging/inbox_surface/` | 5→5 | 不新建文件；read_body/settle_hint 进 `queries.py`（约 630 行）。留出 2 个空位给 WP7 等后续包 |
| `daemon/impl/links/` | 不动 | 规划中已满 7 个（`__init__`、ports、model、controller、policy、store、service），WP3 不得往里加文件 |
| `tests/daemon/inbox/summary/`（新） | 0→5 | 见 §3 |
| `tests/daemon/application/transport/channel/summary/`（新） | 0→7 | 恰在上限，含 `__init__.py` |

单文件最大的是 `retry.py` 约 845、`router.py` 约 858、`application/__init__.py` 约 938，均低于 1000。实施时以 `check_structural_limits.py` 实测为准。

## 2. 公共面与接口

### 2.1 summary 校验规则（`check_summary`）

按顺序判，第一条命中即抛 `DaemonRequestError(SUMMARY_REQUIRED, message, data)`：

| 条件 | `data.reason` | 其他 data |
| --- | --- | --- |
| 缺失或 `None`（仅 `required=True`；`required=False` 时返回 `None`） | `missing` | |
| 不是 `str` | `not_string` | |
| `value.strip() == ""` | `empty` | |
| `value.encode("utf-8")` 抛 `UnicodeEncodeError`（JSON 里的孤立代理项） | `invalid_utf8` | |
| UTF-8 字节数 `> SUMMARY_MAX_BYTES` | `too_long` | `bytes`: 实测字节数，`maxBytes`: 512 |

所有情形 `data` 都带 `"field": "summary"` 与 `"maxBytes": 512`。恰好 512 字节通过。通过后**原样**返回，不 strip、不截断、不归一化。错误信息写明「summary 是唯一进入接收方 context 的部分，必须 ≤512 字节 UTF-8」。

调用顺序：身份认证错误优先于 `SUMMARY_REQUIRED`（未认证的调用方不能借此探测）；`SUMMARY_REQUIRED` 优先于其余一切参数错误与副作用。被拒的 send 不入 outbox、不登记会话、不触发 dispatch gate；被拒的 reply 不 ack 原请求。

### 2.2 人写原文转 summary（`summary_from_text`）

只给「代发人写原文」的组件用（Lark 入站、user-proxy 转递、无上游 summary 的 forward、workflow CLI 通知）。agent 自己的 send/reply 不经过它，send 边界也从不调用它。

- `text` 的 UTF-8 字节数 ≤512：返回 `text` 原样（正文本身就是 summary，不丢信息）。
- 否则：`marker = f"…[全文 {n} 字节，用 read_body 读取]"`，返回「在 UTF-8 字符边界上、使 `前缀字节 + marker 字节 ≤ 512` 的最长前缀」+ `marker`。
- 必须确定性：同一 `text` 永远得同一结果。Lark 入站按同一 `idempotencyKey` 重试，回复的 digest 含 summary，不确定会造成回执冲突。

### 2.3 正文与字节数

- 正文 `body_text(message)`：`payload` 解析为 JSON 对象且 `message` 为字符串时取该字符串；否则 `payload.decode("utf-8", errors="replace")`。
- `body_bytes(message) = len(body_text(message).encode("utf-8"))`。
- pending.list 的 `bodyBytes` 与 read_body 的 `bodyBytes`、`len(read_body.message.encode())` 三者必须相等。

### 2.4 `message.send` / `message.reply` 入参

两者都新增 `summary: string`（A 期可缺省，B 期必填）。其余参数、返回形状不变。多目标 send 共用一个 summary。`message.send` 带 `replyTo` 的路径同样适用。存储：agent 目标的 `InboxMessage.summary`，随 outbox → 线帧（WP0 已加）→ custody → 接收方 inbox 全程保留。

### 2.5 `message.pending.list`（`harness_read` 走同一 IPC）

返回仍为 `{"ok": true, "messages": [...], "daemonEpoch": ...}`。每行：

```
{
  "messageId": str, "deliveryId": str, "conversationId": str,
  "from": str, "to": str, "intent": str,
  "kind": "message" | "notice",
  "summary": str | null,
  "bodyBytes": int,
  "origin": {...},
  "message": str          // 仅 A 期保留，B 期删除
}
```

- `kind = "message"`：inbox 行。`summary` 为存储值；旧行、PAC 投递、自动回复等无 summary 的行为 `null`。载体对 `null` 显示固定提示与 `bodyBytes`，提示 agent 用 read_body（WP5 契约）。
- `kind = "notice"`：system notice。notice 是 daemon 自己写的，读出即删（`drain_system_notices`），之后没法再取正文，所以 `summary` = notice 全文，`bodyBytes = 0`。不受 512 字节限制（不是发送方入参）。
- B 期后行内不再出现正文：没有 `message` 键，也没有 `topic`。
- `fetched: true` 的 session fetch 仍和今天一样记 `fetched_at_ms`、退役发送方回执、发 fetch receipt；要求 `sessionRef` 的规则不变。

### 2.6 `message.read_body`（`IPC_MESSAGE_READ_BODY`）

入参：`messageId: string`（必填，缺则 `INVALID_ARGUMENT`），加上与 `message.ack` 相同的身份参数（`from`/`actor`、可选 `sessionRef`、操作员路径的 `onBehalfOf`）。

身份与 fence：`_authenticated_message_caller(method=IPC_MESSAGE_READ_BODY)`，读键为 `_message_consumer_keys`。带 `sessionRef` 时走同一 fence（失效则 `STALE_SESSION`/`SESSION_SUPERSEDED`）。不带 `sessionRef` 时与 `message.ack` 同规则：允许，但身份必须通过认证（启用强制时未认证即 `SENDER_NOT_AUTHENTICATED`）。理由：单条、幂等、只盖首次时间戳，比 ack 更窄，而 ack 今天就不要求 fence；`pending.list` 要求 fence 是因为它批量提交整箱 custody。

行为：

1. 在读键中找**仍 pending** 的该行：调用 `self._inbox.fetch_pending(key, message_id=messageId)`。命中则该行 `fetched_at_ms` 盖章（`COALESCE`，已盖则保留首次时间）、同 daemon 发送方回执退役；`_fetch_receipt_publisher` 存在时对该条发 fetch receipt（同 pending.list 的 session fetch）。只动这一行，同收件人其他 pending 行不受影响。
2. 未命中则读保留行 `harness_failure_original(messageId)`，且 `row.recipient ∈ 读键`：只读返回，不盖章（已消费或已过期的行）。
3. 都没有（未知 id、别人的消息、已被 TTL 清掉、system notice）：`DaemonRequestError(MESSAGE_BODY_UNAVAILABLE, …, {"messageId": id})`。不泄露该 id 是否属于别人。

返回：

```
{
  "ok": true,
  "messageId": str, "conversationId": str, "from": str, "to": str, "intent": str,
  "summary": str | null,
  "message": str,          // 正文 body_text
  "bodyBytes": int,
  "origin": {...},
  "pending": bool,         // 第 1 步命中为 true
  "fetched": bool          // 返回时该行是否带 fetched 章
}
```

日志：`info`/`daemon`/`message.body_read`，字段 `messageId`、`actor`、`bodyBytes`、`pending`。供上线后统计正文读取率（设计 §11 复测）。

### 2.7 `message.settle_hint`（`IPC_MESSAGE_SETTLE_HINT`）

入参与身份同 read_body。只读，不改任何状态，不在 inbox 权限门的变更操作之列。

判定：

1. `pending = any(item.message_id == messageId for key in 读键 for item in self._inbox.pending_messages(key))`。`pending_messages` 已排除已消费、已过期、终态失败的行。
2. `pending` 为假：`hint = "ack"`（已回复、已 ack、已过期、未知、别人的消息，统一如此；载体照做的 ack 若落空会得到 `MESSAGE_ACK_UNAVAILABLE`，无害）。
3. `pending` 为真且 `self._link_gate is None`：`hint = "keep"`。
4. `pending` 为真且有 `LinkGate`：`hint = self._link_gate.settle_hint(actor=caller.subject, message_id=messageId)`。返回值不在 `{"ack","keep"}` 内时按 `keep` 处理并记 `warn` 日志 `message.settle_hint.invalid`。这一步留给 WP2：link 回收后的残余请求由 gate 答 `ack`（设计 §4.4）。

返回：`{"ok": true, "messageId": str, "hint": "ack" | "keep", "pending": bool}`。类型标注用 `SettleHint`。

### 2.8 worker channel（kernel）

`METHOD_PARAMS` 变化见 §1.1 第 16 行。托管 worker 经该通道调用 read_body/settle_hint 时，`actor`/`sessionRef` 由通道盖章，规则不变。

### 2.9 错误码

| 码 | 何时 | IPC 形状 |
| --- | --- | --- |
| `SUMMARY_REQUIRED` | §2.1 | `error.code`、`error.data`（`field`、`reason`、`maxBytes`，`too_long` 另带 `bytes`） |
| `MESSAGE_BODY_UNAVAILABLE`（新） | §2.6 第 3 步 | `error.code`、`error.data.messageId` |

CLI `--json` 时 stdout 一个对象、必带 `ok`，`ok: false` 退出 1——由 WP6 在 CLI 输出层实现；本包的 IPC 成功返回都带 `ok: true`。

### 2.10 Python 内部接口

- `InboxPort`/facade/service：`fetch_pending(recipient, *, now_ms=None, message_id=None)`。
- `FetchPendingCommand.message_id: str | None = None`。
- `hyprial.daemon` 新导出：`summary_from_text`、`SUMMARY_MAX_BYTES`。
- `DaemonApplication._link_gate: LinkGate | None`。

## 3. 测试清单

「先红」列写实现前该测试的失败形状；实施者须先提交只含测试的提交并保留失败输出（§5）。期别 A/B 表示该测试随哪一期 PR 落地。路径前缀 `tests/daemon/inbox/summary/` 记作 `IS/`，`tests/daemon/application/transport/channel/summary/` 记作 `CS/`。

| ID | 测什么 | 先红形状 | 路径 | 期 |
| --- | --- | --- | --- | --- |
| T1 | 新库三表都有 `summary` 列；旧库（用冻结的 `177929d` 版 DDL 建 `outbox`/`inbox`/`custody`，各插一行）由 `InboxService` 打开后补列、原行 `summary` 为 NULL、其余列不变；再次打开不重复 ALTER | `AssertionError: 'summary' not in {...columns}` | `IS/test_schema_migration.py` | A |
| T2 | 旧行迁移后读取：上述旧 inbox 行经 `message.pending.list` 返回 `summary: null`、`bodyBytes == len("legacy".encode())`、`kind == "message"`；经 `message.read_body` 返回正文 `"legacy"` | `KeyError: 'summary'`（行内无该键） | `CS/test_pending_shape.py` | A |
| T3 | summary 全程保留：带 summary 的消息经 outbox 延迟重试、经 custody 中转、直投三条路径后，接收方 inbox 行与 `pending_messages` 返回的 `InboxMessage.summary` 都等于原值 | `AssertionError: None != 'S'` | `IS/test_schema_migration.py` | A |
| T4 | 校验规则纯函数：缺失（required）、非 str、空白、孤立代理项、512 字节通过、513 字节拒绝且 `data.bytes == 513`；171 个「摘」（513 字节）被拒而 170 个（510 字节）通过——按字节不按字符 | `ModuleNotFoundError: …contracts.summary` | `IS/test_summary_rules.py` | A |
| T5 | `summary_from_text`：≤512 原样；超长时结果 ≤512 字节、以 marker 结尾、marker 内字节数等于原文字节数、不切断多字节字符（`decode` 不报错）、两次调用结果相同 | `ModuleNotFoundError` | `IS/test_summary_rules.py` | A |
| T6 | 超长按字节（经 IPC）：`message.send` 带 513 字节 summary → `DaemonRequestError`，`code == SUMMARY_REQUIRED`，`data.reason == "too_long"`、`data.bytes == 513`、`data.maxBytes == 512`；outbox 与接收方 inbox 计数不变；`message.reply` 同样被拒且原请求仍 pending | send 被接受：`deliveries[0]["accepted"] is True`，未抛错 | `CS/test_send_summary.py`、`CS/test_reply_summary.py` | A |
| T7 | 空白 summary、非 str summary 经 IPC 同样 `SUMMARY_REQUIRED`（`reason` 分别为 `empty`、`not_string`） | 未抛错 | `CS/test_send_summary.py` | A |
| T8 | 缺 summary：`message.send` 不带 `summary` → `SUMMARY_REQUIRED`、`reason == "missing"`；无 outbox/inbox 行；`_dispatch_gate_conversations` 无新键；`message.send` 带 `replyTo` 同样；未认证的会话声明先得到认证错误而不是 `SUMMARY_REQUIRED` | A 期实现下 send 被接受 | `CS/test_send_summary.py` | B |
| T9 | 缺 summary 的 `message.reply` → `SUMMARY_REQUIRED`；原请求仍在 `pending_messages`，`acks` 为空 | 回复被接受，原请求被 ack | `CS/test_reply_summary.py` | B |
| T10 | pending.list 不含 payload：B 期行键集合恰为 `{messageId, deliveryId, conversationId, from, to, intent, kind, summary, bodyBytes, origin}`；任何值都不等于正文；`fetched: true` 与不带 fetched 两种读法形状相同 | `AssertionError: 'message' in row` | `CS/test_pending_shape.py` | B（A 期另有一条断言新增三键存在且 `bodyBytes` 正确） |
| T11 | notice 行：`kind == "notice"`、`summary` 为 notice 全文、`bodyBytes == 0`；对其 `read_body` 得 `MESSAGE_BODY_UNAVAILABLE` | `KeyError: 'kind'` | `CS/test_pending_shape.py` | A |
| T12 | read_body 记 fetched：先用不带 `fetched` 的 pending.list 观察（不盖章，`has_fetched` 为假），再 `read_body` → 正文与 `bodyBytes` 正确、`pending: true`、`fetched: true`、`has_fetched(id)` 为真；同收件人另一条 pending 行仍未盖章；第二次 read_body 不改首次 `fetched_at_ms` | `DaemonRequestError: METHOD_NOT_FOUND … message.read_body` | `CS/test_read_body.py` | A |
| T13 | read_body 身份与 fence：读别人的 pending 消息 → `MESSAGE_BODY_UNAVAILABLE` 且该行未盖章；未知 id → 同码；过期 `sessionRef` → fence 原码；已 ack 的本人消息 → 只读返回 `pending: false`、不报错 | `METHOD_NOT_FOUND` | `CS/test_read_body.py` | A |
| T14 | 单条拉取：`InboxService.fetch_pending(r, message_id=x)` 只返回并盖章 x；同 daemon 发送方对 x 的回执被退役、对 y 的不动；facade 经 `FetchPendingCommand(message_id=x)` 结果相同 | `TypeError: fetch_pending() got an unexpected keyword argument 'message_id'` | `IS/test_fetch_one.py` | A |
| T15 | settle_hint 两态：pending 请求 → `keep`、`pending: true`；`message.ack` 后 → `ack`；`message.reply` 成功后 → `ack`；未知 id → `ack`、`pending: false`；别人的 pending 消息 → `ack`（不泄露）；调用前后 inbox 行与 `fetched_at_ms` 不变 | `METHOD_NOT_FOUND … message.settle_hint` | `CS/test_settle_hint.py` | A |
| T16 | settle_hint 委托 gate：注入假 `LinkGate`（`settle_hint` 恒 `"ack"` 并记录调用）后 pending 请求得 `ack`；非 pending 时 gate 不被调用；gate 返回非法值时得 `keep` | `AssertionError: 'keep' != 'ack'` | `CS/test_settle_hint.py` | A |
| T17 | 重复 reply 仍被拒（digest）：同一 correlation 先后提交两条除 `summary` 外完全相同的回复 → 第二次 `_SubmissionReceiptConflict`；`summary` 都为 None 时 digest 与改动前逐字节相同（钉一个已知 hex） | 第二次被接受，无冲突 | `IS/test_summary_digest.py`（构造方式参照 `tests/daemon/harnesses/actors/test_harness_reply_settlement.py`） | A |
| T18 | 重复 reply 仍被拒（IPC）：首个 reply（summary S1）成功后，同文同 summary 重试 → 加入既有回执、接收方只有 1 条回复；同文不同 summary（S2）重试 → `code == "REPLY_ALREADY_SUBMITTED"`、`retryable is False`；不同正文重试 → 同上；三次之后已提交回复仍只有 1 条 | 不同 summary 的重试返回 `ok: true` | `CS/test_reply_summary.py` | A |
| T19 | 调用路径一致：Lark worker `_Harness.send_request` 发出的参数含 `summary == summary_from_text(text)`；user-proxy 转递与 forward 转递经 `handle("message.send")` 时带 summary（forward 优先用原消息 summary）；`worker_channel.bound_request` 放行 `summary` 与两个新方法、拒绝它们的其他未知参数；`METHOD_PARAMS` 里两个新方法名等于 `IPC_MESSAGE_READ_BODY`/`IPC_MESSAGE_SETTLE_HINT` | `AssertionError: 'summary' not in params`；`WorkerChannelError: Parameter unavailable` | `CS/test_call_paths.py` | A |
| T20 | 路由登记：两个常量都在 `_IPC_STATS_METHODS`；`handle(IPC_MESSAGE_READ_BODY, …)` 不落入 `METHOD_NOT_FOUND`；`_CONSUMER_METHODS` 含两者 | `AssertionError` | `CS/test_call_paths.py` | A |
| T21 | 既有门：`tests/tooling/architecture/authority/test_inbox_authority_gate.py`、`tests/tooling/architecture/migration/test_authority_migration.py`、`tests/daemon/ipc/cost/test_ipc_cost_counters.py`、`tests/daemon/ipc/error/test_ipc_error_code_registry.py`、`tests/daemon/links/test_wp0_seams.py` 全绿 | 加 read_body 后、改 crosswalk 前：`unmanifested inbox bypass: …queries.py:_MessageQueriesMixin._ipc_message_read_body:fetch_pending` | 既有路径 | A |

每个新测试目录带 `__init__.py`。`IS/`：`__init__.py`、`test_schema_migration.py`、`test_summary_rules.py`、`test_fetch_one.py`、`test_summary_digest.py`。`CS/`：`__init__.py`、`test_send_summary.py`、`test_reply_summary.py`、`test_pending_shape.py`、`test_read_body.py`、`test_settle_hint.py`、`test_call_paths.py`。应用级测试复用 `tests/daemon/application/transport/channel/support.py` 的 `application`/`register`/`FakeInbox`，需要真 SQLite 的（T2、T12、T13、T15）照 `tests/daemon/delivery/origin/test_inbound_origin.py::_app` 用真 `InboxService`。所有 home、socket、数据库都在 `tmp_path`。

## 4. 验收标准（可机检）

A、B 两期都要满足 1 至 7；B 期另加 8、9。

1. 焦点测试全过：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/inbox/summary tests/daemon/application/transport/channel/summary tests/daemon/links tests/daemon/ipc tests/tooling/architecture/authority tests/tooling/architecture/migration -q`，0 failed、0 error。A 期新测试至少 T1–T7、T10（A 期断言）、T11–T21；B 期加 T8、T9、T10。
2. 全量：`uv run --with libcst==1.9.0 pytest tests/` 的失败集合 ⊆ 同机在基线 `177929d26` 上的失败集合（基线已知 10 条，其中 `test_registry_membership_is_frozen` 应被本包修绿，其余 9 条为 GUI、pi loader、平台、CLI 清单类环境失败）。用 `-rf` 输出的 `FAILED` 行做集合比较，比较结果贴进 PR。
3. `uv run ruff check .` 无输出。
4. `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 与 `scripts/ci/check_module_boundaries.py` 均无违规。
5. `git diff --exit-code origin/refactor/org-structure-constrain -- tests/inbox_authority_exemptions.json`（清单只缩不增）。
6. `rg -n '"message\.(read_body|settle_hint)"' src/hyprial` 只命中 `kernel/impl/contracts/channels/worker_channel.py`；router、visibility 用 ports 常量。
7. PR 正文有 `Contract-Impact:` 行写明延后到 WP8 的条目（§6），`scripts/verify/check_contract_impact.py` 通过；有 `Skill-Impact: none（WP8 更新 hyprial-ops）`。
8. （B）`rg -n '"message": text' src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py` 无输出；`rg -n 'required=False' src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py` 无输出。
9. （B）合并时集成分支已含 WP5、WP6 的合并提交（`git log --merges` 可见），且 2 在 B 分支上成立。

## 5. 要留的证据（贴 PR 正文）

1. 先红：只含测试的提交 sha，以及在该提交上运行焦点测试的 `FAILED` 行（每条新测试的失败形状应与 §3「先红」列一致；不一致要在 PR 里说明）。
2. 后绿：实现提交上同一命令的结果行。
3. 旧库迁移实测：在 `tmp_path` 里用基线 `177929d26` 的代码（`git worktree` 检出）建一个 inbox 库并写一条消息，再用 WP3 代码打开，贴 `PRAGMA table_info(inbox|outbox|custody)` 的 `summary` 行、该消息的 pending.list 行与 read_body 结果。绝不使用生产 daemon 的库。
4. 全量测试的失败集合比较（验收 2）、ruff、两道结构门的输出。
5. 字节规则实测：513 字节 summary 的 IPC 错误对象原文（含 `data.bytes`）。
6. B 期另贴：§0 那组失败文件在 B 分支上的通过结果，以及 CLI `hyprial send` 缺 `--summary` 时的 `--json` 输出（WP6 已合并后才可测）。

未实测项写 UNKNOWN 或 BLOCKED，不用 skip 充当结论。

## 6. 对 contract/ 的影响与 WP8 同步清单

本包改变已发布字段语义：信封 `summary`（存储与线帧）、`message.send`/`message.reply` 入参、`message.pending.list` 返回形状、两个新 IPC 方法、`SUMMARY_REQUIRED` 的 `data` 形状、新码 `MESSAGE_BODY_UNAVAILABLE`。按 2026-10-14 在 `contract/` 下 grep `message.send`、`message.reply`、`pending.list`、`harness_read/send/reply`、`"payload"`、行内 `["message"]`/`.get("message")` 的结果，受影响处：

| 区域 | 文件 | 受影响原因 | WP8 要做 |
| --- | --- | --- | --- |
| wire golden | `contract/wire/golden/envelopes.json`、`expectations.json`、`transport-inbox.json`、`validate.py`、`run-python.py`、`run-transport-inbox.py`；`src/hyprial/daemon/impl/ipc/wire.py`（契约跑者从信封构造 `InboxMessage`） | 信封无 `summary`；无缺 summary、超长、pending 不含正文的用例 | request/reply 夹具加 `summary`；新增用例：缺 summary 拒绝、513 字节拒绝且报字节数、pending 行无正文、read_body 记 fetched、settle_hint 两态；`ipc/wire.py` 映射 `summary`（这是 src 改动，WP8 一并做或单开） |
| wire 说明 | `contract/wire/message-sender-auth.md`、`trajectory.md` | 列出 `message.send` 参数与身份规则 | 加 `summary` 参数、两个新方法的身份规则（同 `message.ack`） |
| MCP 工具 | `contract/mcp-tools/run.py`（`harness_send`/`harness_reply` 调用无 summary；`factory.calls` 期望列表）、`contract/mcp-tools/pi-receipts.md` | WP6 给工具加必填 `summary` 与 `harness_read_body` | 调用补 `summary`；期望列表加 `message.read_body` |
| 黑盒 | `contract/cli-blackbox/run.py`、`contract/daemon-lifecycle/run.py` | 经 CLI send 或读 pending 行 | `--summary`；断言改读 `summary`/`bodyBytes` |
| worker 协议 | `contract/workers/python-worker/protocol.schema.json` 与 `fixtures/*.json`、`contract/workers/user-proxy/protocol.schema.json` 与 `fixtures/*.json` | 帧里有 message/payload 字段 | 核对是否携带 pending 行正文；若是，改为 summary + read_body |
| e2e 发送 | `run-actor-registration-roundtrip.py`、`run-codex-app-server.py`、`run-cc-tui-worker.py`、`run-dsh-worker.py`、`run-codex-worker.py`、`run-daemon-restart-survival.py`、`run-dual-instance-roundtrip.py`、`run-hosted-start.py`、`run-hosted-upgrade.py`、`run-outbox-deadletter.py`、`run-orgfs-forwarding-redial.py`、`run-pac-graph-slice.py`、`run-pi-interactive-attach-manual.py`、`run-peer-restart-relearn.py` | `hyprial send` 无 `--summary` | 全部补 `--summary` |
| e2e 读行 | `run-selfdrive-routine.py`、`run-pac-public-events.py`、`run-dsh-worker.py`、`run-routine-template-pac-journal.py`、`run-resident-providers.py`、`run-pac-graph-slice.py`、`run-codex-worker.py`、`run-cc-tui-worker.py`、`run-actor-registration-roundtrip.py`、`run-daemon-restart-survival.py`、`_harness_lifecycle/phases/queue.py`、`_harness_lifecycle/phases/lark.py`、`_harness_lifecycle/processes.py`、`_upgrade_continuity/inbox.py` | 从 pending 行取 `message` | 匹配改用 `summary`，需要正文的改调 `message.read_body` 或 `hyprial read` |
| e2e 其他 | `contract/e2e/scenarios.json`、`01-two-machine-roundtrip.md`、`06-resident-providers.md`、`08-cc-tui-worker.md`、`13-all-harness-lifecycle.md`、`14-dsh-worker.md`、`README.md`、`_resident_evidence.py`、`_harness_lifecycle/lark_messages.py`、`feishu/transports/gateway.py`、`run-all-harness-lifecycle.py`、`run-squire-setup.py`、`run-pi-wake-registration.py`、`run-upgrade-continuity.py` | 文档与跑者描述 send/reply/read 的形状 | 同步文字与命令 |
| 其他说明 | `contract/inbox-watch/README.md`、`contract/pac/context/README.md` | 描述 pending 读法与 PAC 投递 | 说明 PAC 投递 `summary` 可能为 null（见 §7.4） |

WP8 另需：新增一个升级连续性场景，跑基线版本写入的 inbox 库在新版本下可读（对应 T1/T2 的真实环境版）。

## 7. 对缝的建议与开放项

1. **WP0 留下的红**：`tests/daemon/ipc/error/test_ipc_error_code_registry.py::test_registry_membership_is_frozen` 在 `177929d26` 上已失败（6 个新码没有加进 `FROZEN_CODES`）。所有并行包的 PR CI 都会因此变红。建议在集成分支上立即单独修复；若未修，WP3-A 按 §1.2 第 25 行一并修。
2. **新错误码**：read_body 需要 `MESSAGE_BODY_UNAVAILABLE`，WP0 词汇里没有。WP3 在 kernel 新增（kernel 只加错误码，符合设计 §14）。
3. **分期**：建议把 README 工作包表改为：WP5 依赖「WP0 与 WP3-A 的 IPC 形状」；新增 WP3-B 依赖「WP3-A、WP5、WP6 合并」；WP8 依赖 WP3-B。
4. **不经 `message.send` 的投递**：PAC 的 `InboxIo.deliver`（`inbox/links/io.py`，由 `DaemonPacNotificationSender` 调用）直接提交 `InboxMessage`，`runtime/forwards.py` 的 agent-sdk 自动回复也是。WP3 之后它们的 `summary` 为 NULL，载体显示为「无摘要」。自动回复由 WP5 删除；PAC 投递需要一个 owner：建议给 `NotificationSender.send` 与 `InboxIo.deliver` 加必填 `summary`，由 PAC 规划方生成。可并入 WP5，或单开 WP3c。在此之前，设计 §4.1 的「没有豁免」对 PAC 不成立。
5. **人写原文的 summary**：§2.2 的规则（≤512 原样，超长时确定性前缀加显式 marker）由代发组件使用，send 边界本身从不截断。评审需要确认这不违反设计 §7 的「不截断」。否则 Lark 入站无法合规（人不会写 summary）。
6. **import 方向**：`inbox/contracts/summary.py` 与 `ipc/router.py` 从 `hyprial.daemon.impl.links.ports` 取常量，会执行 `links/__init__.py`。WP2 往 `links/__init__.py` 加导出时，不得从那里 import `service.py` 或任何 import inbox 的模块，否则成环。建议在 `test_wp0_seams.py` 加一条「import `links.ports` 不加载 `links.service`」的断言。
7. **`LinkGate.settle_hint` 的语义**：WP3 只在 inbox 判定仍 pending 时才问 gate（§2.7）。WP2 的实现只需回答「仍 pending 的请求是否已成回收残余」，不必重复判断 pending。
8. **kernel 字面量**：`worker_channel.py` 不能 import daemon 常量，两个新方法名在 kernel 写字面量，由 T19 钉住与 ports 常量相等。若想彻底去重，可把 `IPC_MESSAGE_*` 常量下沉到 kernel 合约，由 ports 再导出（改缝，需 WP0 owner 同意）。
9. **`message.query`**：操作员视图（`_query_entry`）仍显示正文。它不进 agent context，本包不改。若要求「agent 任何路径都拿不到批量正文」，需另行限制 `message.query` 的调用方。

## 8. 评审

结论：**BLOCKING:5 ADVISORY:4**。下列修正规格覆盖正文中的冲突文字。
本次评审覆盖 A/B 规格；WP3-B 由独立节点执行。
评审基线为本分支 `2120854e3`，不是历史基线 `177929d26`。

### 阻断 R1：超长 summary 的错误字段与缝不一致

- 位置：§2.1、§2.9、T4、T6、§5 第 5 条。
- 问题：规格要求 `data.bytes`，且附加 `field`、`reason`。
  设计 §9 与 README 裁定 16 已规定超长错误的精确形状。
  ports 的 `SUMMARY_REQUIRED_DATA_KEYS` 是 `("summaryBytes", "maxBytes")`。
- **修正规格：**
  - summary 超过预算时，必须抛 `DaemonRequestError(SUMMARY_REQUIRED, …, data)`。
  - `data` 必须恰为 `{ "summaryBytes": n, "maxBytes": SUMMARY_MAX_BYTES }`。
    `n` 是 UTF-8 字节数；不得另加 `bytes`、`field` 或 `reason`。
  - 缺失、类型错误、空白、孤立代理项仍按 §2.1 拒绝。
    这些非超长错误可保留正文规定的诊断字段。
  - T4、T6 必须断言超长错误的完整 `data`，不只断言单个键。
    §5 的证据必须记录 `summaryBytes`。§6 的契约清单同步改名。

### 阻断 R2：新回复未保存显式关联，无法落实 §4.5

- 位置：§1.1 第 11 行、§2.4、T8、T9、T18。
- 问题：现有 agent send 只用 `replyTo` 改 `intent`，没有保存原 id。
  `message.reply` 也只在 adapter 回复正文中写 `replyTo`。
  只增加 summary 会保留这个缺口。
  接收端无法据此闭合原出站事项或测量该 link 的 RTT。
  相同 conversation 或 `intent="reply"` 均不能代替显式关联。
- **修正规格：**
  - 新建 `message.send(replyTo=id)` 与 `message.reply(messageId=id)` 的回复时，
    必须把原 id 保存在 payload 的 `replyTo` 字段。
    此规则同样适用于 agent 目标，不只适用于 adapter。
  - 关联必须随 outbox、custody、wire 与 inbox 保存。
    gate 入参中的 `reply_to` 必须取同一个 id。
    WP3 负责消息映射；WP2 负责 gate 接线与计数。
  - 不带 `replyTo` 的 send 必须仍是新请求。
    不得用 conversation、正文、目标或 messageId 形状推断回复。
  - 对既有回执的重试必须保留原 command，不能改写已存 payload 或 digest。
    `summary=None` 的历史 digest 兼容断言仍须保留。
  - 新增测试：两种显式回复跨三表及线帧后仍有原 id；
    普通 send 复用 conversation 时没有 `replyTo`，原请求仍 pending。
    两种回复的 summary 拒绝都不得 ack 原请求。
  - 与 WP2 集成时必须补测：W/cwnd 满时有效回复能闭合事项；
    已回收排干 id 的回复得到 `LINK_RECLAIMED`，且没有新投递或 ack 副作用。
    未知 id 沿用 `MESSAGE_REPLY_UNAVAILABLE`。
    WP3 必须通过 `LinkGate` 协议判定，不得导入 `links.service`。

### 阻断 R3：read_body 能绕过互引等待和 W 准入

- 位置：§1.1 第 6、12 行、§2.6 第 1、2 步、T12–T14。
- 问题：现有 `fetch_pending` 只判 recipient、TTL 与终态。
  规格让新 IPC 直接调用 `fetch_pending`，没有判定 link 与 W。
  即使后续给 fetch 加过滤，`harness_failure_original` 仍能读出 held 行。
  这个函数实际执行无状态过滤的 `SELECT * FROM inbox WHERE message_id = ?`。
  分区副本和 W 满时，held 消息可能通过新 IPC 进入 context。
- **修正规格：**
  - 对仍 pending 的 agent 间消息，read_body 必须先通过 consumer admission。
    仅通过注入的 `LinkGate.admit_dispatch` 判定；不得导入 service。
    入参必须包含规范 recipient、sender、message_id、intent 与显式 reply_to。
  - 对尚未准入的受管消息，互引未同步、W 无余量或 gate 未绑定时，
    不得返回正文。已准入消息的重复读取遵守 ports 的幂等规则。
    返回 `MESSAGE_BODY_UNAVAILABLE`，不得盖 fetched、退役回执或发 fetch receipt。
    无 gate 时的 agent 间行为遵守 README 裁定 26。
  - owner、adapter、system、自发自收的豁免必须复用 WP4 的边界分类。
    有效回复的 W 豁免必须由 policy 判定，不能由 IPC 猜测。
  - admission 必须按 `(recipient, message_id)` 幂等。
    同一消息先读 summary、再读 body、再读 body，不得重复占 W。
  - retained-row 回退必须同时核验 recipient 与权威消费状态。
    仅已消费、已过期或终态保留行可走只读回退。
    仍 pending 但 admission 未通过的行不得走回退。
  - read_body 只记录消费，不闭合请求、不 ack、不作为发出回复的 T 样本。
    请求继续占 W，轮末 settle_hint 仍为 keep，直至回复、ack 或放弃。
  - 新增假 gate 测试：W 满、互引延迟、未绑定 gate 时正文不可读且状态不变；
    gate 从 false 变 true 后，同一 id 自动可读且原正文、summary 未丢。
    另测重复读只准入一次、owner 不受 W 限制、有效回复不受 W 限制。
    held 行必须特别验证不能经 retained-row 回退泄露。
  - WP4 负责 pending.list/wait 的准入；WP3 负责新 read_body 的同等约束。
    两处必须使用同一分类与协议，不创建第二套 link 权威。

### 阻断 R4：用 ack 的规则授权 fetch，会重新打开 custody fence 缺口

- 位置：§2.6 的“允许不带 sessionRef”、T12、T13。
- 问题：read_body 第 1 步会盖 fetched、退役发送方回执并发 fetch receipt。
  这与 ack 的操作语义不同。
  现有 pending.list 明确拒绝无 sessionRef 的显式 fetch。
  `_sender_auth_enforced=False` 时，IPC 的 agent 声明仍能通过认证观察路径。
  按原规格，这个声明能借 read_body 提交另一个会话的 custody。
- **修正规格：**
  - IPC 上的 agent 消费者读取 pending body 时，必须提供有效 sessionRef。
    即使发送者认证处于观察模式，也不得绕过这个要求。
  - 未提供 fence 时，必须在 fetch、回执退役及发布之前返回 `STALE_SESSION`。
    提供过期或已替换的 fence 时，保留现有 fence 错误码。
    身份认证已拒绝时，仍优先返回认证错误。
  - 已核验的本机 owner 运维代读，以及可信 daemon/adapter 内部调用，
    可按现有认证边界访问。不得仅凭参数声明这些身份或可信 origin。
    运维代读必须按 `caller.subject` 限定 consumer keys。
  - CLI 的 managed-worker read 必须携带现有 worker 凭据与 fence。
    这个要求交给 WP6；不得为 CLI 另开无凭据的 agent 路径。
  - 新增 IPC 测试必须使用真实 `_trusted_message_origin="ipc"`：
    认证强制开启时的未认证调用、观察模式下的无 fence 调用、
    伪造 fence、过期 fence、superseded fence、有效 worker fence。
    所有拒绝均须断言 fetched、sender receipt 和发布器调用数不变。
  - read_body 与 settle_hint 还须覆盖 owner 的 onBehalfOf 与别人的消息。
    gate 的 actor 必须是已认证的规范 subject，不是原始 `from` 或 alias。

### 阻断 R5：全量验收把缺 GUI 环境当作可通过的基线失败

- 位置：§4 第 2 条、§5 第 4 条。
- 问题：旧基线的 10 条失败已不是当前基线事实。
  按失败集合包含关系验收，会把缺 GUI 依赖或低 Node 版本算作通过。
  这违反 AGENTS.md 的测试纪律。
- **修正规格：**
  - 实施者必须在实际实施 base 上重测，不得复用历史失败清单。
    全量前必须按 AGENTS.md 安装 GUI 依赖并使用 Node 24。
  - 焦点测试、两道结构门与改动文件 lint 必须全部通过。
    全量 `uv run --with libcst==1.9.0 pytest tests/` 必须报告真实结果。
  - 缺依赖、低版本 Node 或真实环境不可用时，结果必须标为 BLOCKED。
    不得用 skip、xfail 或历史基线失败将这些项目改为 PASS。
  - 同机 base 的比较可证明没有新增回归，但不能替代全量通过。
    若 base 仍有失败，逐项记录原因与影响，交集成者裁定。
    在裁定前不得写“全量验收通过”。

### 建议 A1：删除已被集成者解决的开放项

§1.1 第 17、20 行、§1.2 第 25 行与 §7 第 1–6、8 条需要更新。
当前基线已有 `MESSAGE_BODY_UNAVAILABLE`、冻结码登记、公共面常量导出与轻入口守卫。
宜只新增 `summary_from_text` 的公共面导出，不重复导出 `SUMMARY_MAX_BYTES`。
A/B 分期、PAC 投递归 WP5、确定性派生 summary 均已有 README 裁定。
B 分支以当前工作流的 `wp3b/send-links` 为准。
当前派单只允许本地提交；§0、§4、§5 的 push/PR 操作不适用于本节点。

### 建议 A2：T10 的“任何值不等于正文”会拒绝合法消息

设计允许 summary 与短正文相同；§2.2 还明确要求短文本原样返回。
宜用不同的 summary/body 哨兵测试正文不泄露。
另加 `summary == body` 的合法用例。
断言键白名单、没有 payload/message/topic、summary 未由读取端推导，
不得对任意值与正文作不等断言。

### 建议 A3：补充字节数与后台重试的测试

宜为 T3、T12、T18 补充这些用例：

- summary 与正文含多字节字符、换行；JSON 非对象、message 非字符串、非 JSON payload。
  对这些用例核验 §2.3 的 fallback 与两处 bodyBytes 一致。
- queued reply 经过后台重试和 daemon 重启后，summary 与 digest 保留。
  同文同 summary 能加入原回执，换 summary 仍得到 `REPLY_ALREADY_SUBMITTED`。
- A 期明确测试缺 summary 仍成功；B 期同一场景必须拒绝。
  新测试目录的文件数仍不得超过 7。

### 建议 A4：明确 §5.6 的跨包验收归属

控制器归 WP1；WP3 不得在 inbox 或 shell 增加 W/cwnd 估计器。
R2、R3 的补测覆盖回复解死锁、owner 优先级、分区 hold 与只读不闭合。
宜在 WP8 的集成清单中补齐以下证据归属：

- WP1：服务率下降、非拥塞慢、基线到期、后来者公平、冷启动。
- WP2：放弃阈值释放、审查样本按阈值计入、link 最短存活期与高抖动 hold。
- WP2/WP4：副本重新同步后放行、owner 在 W 满时仍可注入。

放弃阈值释放后不得自动重发；读取 body 不得改变放弃阈值。
跨包集成未执行时必须记 UNKNOWN，不能用 WP3 单元测试代替。

### 评审实测

- PASS：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links/test_wp0_seams.py tests/daemon/ipc/error -q`，10 passed。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`，退出 0。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`，退出 0。
- 依赖检查：规格内 kernel 保留方法名字面量，由测试与 ports 常量核对。
  shell 经 `hyprial.daemon` 公共面取 helper，方向符合五层约束。
  inbox/router 导入 ports 属 daemon 域内接线，未要求 links 导入 inbox service。
- PASS：`git diff --check`，无空白错误。
- N/A（仅 Markdown 改动）：`uv run --with ruff ruff check --force-exclude --exclude '*.md' docs/design/send-links-impl/wp3.md`，退出 0；没有 Python lint 对象。
- UNKNOWN：WP3 行为测试和全量测试。本次只改评审文档，产品尚未实施。

## 9. 评审 Stage B

结论：**BLOCKING:3 ADVISORY:5**。评审基线为本分支 `526ee8f73`（集成分支已含 WP3-A、WP5-A、WP5-C 三载体、WP5-D、WP6、WP2）。

实测方法（本评审的全部红集结论都来自这次模拟，不是推测）：在干净头上把 `send.py` 两处 `required=False` 改为 `required=True`、`queries.py` pending 行删去 `"message"` 键，跑 `PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon -q --no-header -p no:cacheprovider -rf`（233 failed / 6616 passed）、`tests/shell tests/kernel tests/integration`（89 failed / 3914 passed）、`tests/tooling/architecture`（9 failed / 304 passed）；然后 `git checkout --` 还原，对并集 55 个红文件重跑同命令（30 failed / 798 passed，全部为本机环境类既有红）。Stage B 净增 **301 条、44 个文件**。

### 阻断 B1：§1.3 缺裁定 40 的 `ipc/wire.py` summary 映射条目

- 位置：§1.3、§6。
- 问题：裁定 40 明确「`ipc/wire.py` 对 `summary` 的映射属 WP3-B（硬切前必须到位），不留给 WP8」，但 §1.3 的改动表只有 send.py 两处翻转与 queries.py 删键；§6 的旧文字（「WP8 一并做或单开」）被裁定 40 推翻后没有回头改 §1.3。实测 `wire.py` 的 `_message()`（L162）从信封构造 `InboxMessage` 时不传 `summary`，`envelopes.json` 现有夹具也无 `summary` 字段。实施者照 §1.3 做会漏掉这项硬切前置。
- **修正规格：** §1.3 增加一行：`ipc/wire.py` `_message()` 在构造 `InboxMessage` 时映射信封的 `summary`（`envelope.get("summary")`，非 str 时忽略）。映射必须加性：无 `summary` 字段的既有 golden 夹具继续以 `summary=None` 过界，行为与今天逐字节相同，WP3-B 不触碰 `contract/` 也全绿；新增 golden 用例（缺 summary 拒绝、513 字节拒绝、pending 行无正文、read_body 记 fetched）与夹具 `summary` 字段仍归 WP8（X6 的 `hyprialOnly` 标记）。`_WG_015` 的 capacity blocker 等永不进 pending 行的内部构造不要求带 summary。

### 阻断 B2：§0 的 B 期测试清单已过时，实测为 44 文件 301 条，须以实测清单替换

- 位置：§0「明细（B 期要改的现有测试）」与 §1.3 第三行。
- 问题：§0 的清单是 WP0 时点的量级估计。实测：其中 7 个文件已被上游修好（现绿），另出现 8 个 §0 未列的红文件（WP2/WP5/WP6/WP7 合入后的新测试），多个文件的条数也变了。§1.3 只写「§0 列出的现有测试」会让实施者漏改。
- **修正规格：** §0 的 B 期明细替换为下表（方法与本节开头相同；「类」S = 缺 summary 被拒，K = 读已删除的行内 `message` 键）：

| 文件 | 条数 | 类 | 状态说明 |
| --- | --- | --- | --- |
| `tests/daemon/squire/addressing/routing/delivery/test_agent_bot_round2.py` | 28 | S | §0 原列 28，仍红 |
| `tests/kernel/actors/identity/test_resolution.py` | 24 | S | §0 原列 22 |
| `tests/daemon/routing/test_route_outbound.py` | 23 | S | §0 原列 14 |
| `tests/daemon/squire/addressing/routing/delivery/test_agent_bot_review.py` | 19 | S | 仍红 |
| `tests/daemon/path_authz/test_send_gate.py` | 18 | S | 仍红 |
| `tests/daemon/links/service/test_wiring.py` | 18 | S | **§0 未列**（WP2 接线测试） |
| `tests/daemon/squire/addressing/routing/delivery/test_agent_bot_dm.py` | 11 | S | 仍红 |
| `tests/daemon/application/transport/channel/delivery/test_delivery.py` | 10 | S | 仍红 |
| `tests/daemon/application/messaging/test_open_requests_from.py` | 10 | S | **§0 未列**（WP7） |
| `tests/daemon/identity/aliases/test_alias_input_surfaces.py` | 9 | S | 仍红 |
| `tests/daemon/delivery/sender/test_visibility_sender_canonical.py` | 9 | S | 仍红 |
| `tests/daemon/application/ports/test_daemon_capability_wiring.py` | 9 | S | 仍红 |
| `tests/integration/actor_preservation/delivery/channel/test_reply_settlement.py` | 11 | S | 仍红 |
| `tests/kernel/actors/identity/test_mailboxes.py` | 7 | S | 仍红 |
| `tests/integration/identity/contracts/test_identity_contract.py` | 7 | S | 仍红 |
| `tests/daemon/dispatch/without/test_dispatch_without_pac.py` | 7 | S | 仍红 |
| `tests/daemon/delivery/message/test_authenticated_message_sender.py` | 7 | S | 仍红 |
| `tests/daemon/squire/addressing/routing/adapter_contracts/test_adapter_contracts.py` | 6 | S | 仍红 |
| `tests/daemon/routing/test_route_attribution_targets.py` | 5 | S | 仍红 |
| `tests/daemon/routing/addressing/test_agent_address_normalization.py` | 5 | S | 仍红 |
| `tests/daemon/dispatch/gate/test_dispatch_gate.py` | 5 | S | 仍红 |
| `tests/daemon/delivery/origin/test_inbound_origin.py` | 5 | K | 仍红（§0 原列 2 条 K） |
| `tests/shell/compatibility/test_target_kind_honesty.py` | 5 | S | 仍红 |
| `tests/tooling/architecture/identity/test_sender_identity_gate.py` | 9 | S | 仍红 |
| `tests/daemon/application/ports/test_daemon_visibility_wiring.py` | 4 | S | 仍红 |
| `tests/daemon/inbox/idempotency/test_agent_send_idempotency.py` | 3 | S | 仍红 |
| `tests/daemon/workers/test_worker_relay.py` | 2 | S | 仍红 |
| `tests/daemon/transport/first/test_local_first_self_node_delivery.py` | 2 | S | 仍红 |
| `tests/daemon/squire/addressing/routing/delivery/test_delivery.py` | 2 | S | 仍红（§0 原列 13） |
| `tests/daemon/delivery/identity/test_sender_identity.py` | 2 | S | 仍红 |
| `tests/daemon/application/transport/channel/summary/test_send_summary.py` | 2 | S | WP3 自己的 A 期放行测试，B 期翻转为 T8/T9：`test_a_send_without_a_summary_still_succeeds_in_phase_a`、`test_a_reply_without_a_summary_still_succeeds_in_phase_a` |
| `tests/daemon/application/transport/channel/summary/test_pending_shape.py` | 2 | K | WP3 自己的 A 期断言，B 期翻转为 T10：`test_a_pre_summary_row_reads_as_null_summary`、`test_the_a_phase_row_adds_summary_and_body_bytes`（改断言键白名单恰集） |
| `tests/daemon/application/transport/channel/routing/test_routing.py` | 2 | S+K | 仍红（K：`test_system_notice_is_returned_once_without_ack_or_fifo_settlement`） |
| `tests/integration/pac/events/test_pac_events_blackbox.py` | 2 | S | **§0 未列** |
| `tests/integration/actor_preservation/delivery/channel/test_registration.py` | 2 | K | 仍红 |
| `tests/daemon/squire/addressing/routing/receiver_guidance/test_receiver_guidance.py` | 1 | S | 仍红 |
| `tests/daemon/ipc/cost/test_ipc_cost_counters.py` | 1 | S | §3 T21 点名的既有门，夹具 send 需补 summary |
| `tests/daemon/harnesses/pi/headless/test_pi_headless_worker_fetch_e2e.py` | 1 | S | 仍红 |
| `tests/daemon/harnesses/carrier/turns/test_turn_reports.py` | 1 | S | **§0 未列** |
| `tests/daemon/channels/mcp/lifecycle/test_mcp_channel_owner_migration_e2e.py` | 1 | S | 仍红 |
| `tests/daemon/channels/mcp/contracts/test_mcp_daemon_alignment.py` | 1 | S | 仍红 |
| `tests/daemon/application/transport/channel/sessions/test_sessions.py` | 1 | K | 仍红 |
| `tests/kernel/actors/identity/test_wire_delivery.py` | 2 | S | 仍红 |

  已被上游修好、B 期不再红的 §0 文件（实测绿）：`test_lark_submission_outcome_unknown.py`（19 passed）、`test_proxy_forward_attribution.py`、`test_lark_unknown_review_controls.py`、`test_forward_as_actor.py`、`test_interactive_session_fetch_contract.py`、`test_lark_pending_notice_claim.py`、`test_lark_daemon_idempotency.py`。
  本机干净头既有红 30 条（与 Stage B 无关，验收 2 对比时剔除）：`tests/shell/squire/setup/` 四文件 20 条（`--machine` 与 `HYPRIAL_NODE_ID` 不匹配，环境类）、`tests/shell/gui/lifecycle/` 三文件 5 条、`tests/shell/login/device/test_boundaries.py` 1 条（t11）、`tests/kernel/platform/test_platform_actor_preservation.py` 1 条、`tests/integration/native_harnesses/test_native_runtime_entries.py` 1 条、`tests/integration/actor_preservation/delivery/test_actor_preserved_provider_failure_settlement.py` 1 条、`tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py` 1 条。实施者仍须按 §8 R5 在实际实施头重测，不复用本表。

### 阻断 B3：availability_loud 两条 durable 通知不带 summary，直接 submit 进 agent inbox

- 位置：§0「不在 WP3 内」的豁免清单、§7.4；产品代码 `src/hyprial/daemon/impl/correlation/availability_loud.py` 与 `src/hyprial/daemon/impl/runtime/forwards.py`。
- 问题：硬切后仍有无 summary 的产品调用方。`unavailable_notice` 与 `no_progress_notice` 构造 `intent="reply"`、`DURABLE_SERVICE` 的 `InboxMessage`（无 `summary`），经 `_submit_notice_to_inbox`（forwards.py L525 → `self.inbox.submit`）投给 agent 与 user-proxy 收件人。它们在 pending.list 里是 `kind="message"`、`summary: null` 的行：接收 agent 只看到「no summary」+bodyBytes，必须 read_body 才知道请求失败原因。§0 豁免清单（PAC 投递、自动回复、调用方入参）与裁定 31 的两条绕行路径都不含它；同文件的机制应答（hook、python_worker）已带 `summary_from_text`（forwards.py L757、L798），唯独 loud 通知漏了。
- **修正规格：** 归 WP3-B：`availability_loud.py` 的 `_message()` 增加必填 `summary` 参数，两个通知构造处传 `summary_from_text(text)`（`text` 在 `_message` 内已有；nonce 在 payload 不在 text，派生确定，重试幂等不受影响）；`forwards.py` 三个调用点透传。测试：`CS/test_call_paths.py` 加两条——两个通知经 `inbox.submit` 后 `summary` 非空且 ≤512 字节；`_submit_loud_to_human` 的人达路径不受影响。

### 建议 A1：删键后的死代码与验收 8 的 rg 覆盖

实测：删去 pending 行 `"message"` 键后，`queries.py` 的 `text = body.get("message", …)`（L277-278）成为死赋值，`uv run ruff check` 报 `F841`；`body` 仍被 `_visible_message_origin` 使用须保留。§1.3 应写明：删键同时删 `text` 赋值；验收 3（ruff）会强制这一点，不必改验收 8。

### 建议 A2：E2E 跑者实测清单（24 发 / 13 读），WP8 §3.6 边界不变

- 发送侧（无 `--summary`，20 个 `run-*.py` + 4 个共享 helper）：`run-actor-registration-roundtrip`、`run-cc-tui-worker`、`run-codex-app-server`、`run-codex-worker`、`run-daemon-restart-survival`、`run-dsh-worker`、`run-dual-instance-roundtrip`、`run-hosted-start`、`run-hosted-upgrade`、`run-orgfs-forwarding-redial`、`run-outbox-deadletter`、`run-pac-graph-slice`、`run-peer-restart-relearn`、`run-pi-interactive-attach-manual`、`run-pi-wake-registration`、`run-resident-providers`、`run-selfdrive-routine`、`run-tui-attach-manual`、`run-two-machine-roundtrip`、`run-zenoh-half-open-reconnect`，加 `_harness_lifecycle/phases/lark.py`、`_harness_lifecycle/phases/queue.py`、`_harness_lifecycle/processes.py`、`_upgrade_continuity/scenario.py`。§6 的发送清单少列了 7 个（two-machine、tui-attach、zenoh-half-open、resident-providers、selfdrive-routine、pi-wake-registration、pac-public-events 侧的 helper），WP8 §3.6 以本清单为准。
- 读侧（读行内 `message`，13 个文件）：`run-cc-tui-worker`、`run-codex-worker`、`run-dsh-worker`、`run-pac-graph-slice`、`run-pac-public-events`、`run-selfdrive-routine`、`run-routine-template-pac-journal`、`run-actor-registration-roundtrip`、`run-daemon-restart-survival`（notice 行）、`run-resident-providers`、`phases/lark.py`、`phases/queue.py`、`_harness_lifecycle/processes.py`。
- 边界：`contract/` 归 WP8（X9：E2E-026 为 CI 门、E2E-027 允许 NOT-RUN），WP3-B 只改 `src/` 与 `tests/`；硬切合入到 WP8 §3.6 落地之间 contract/e2e 是红的，这是裁定 76 已接受的窗口，PR 正文写明即可。contract 内其余跑者已就绪：`mcp-tools/run.py` 调用已带 summary，`cli-blackbox/run.py` 已测本地缺 summary 拒绝，`wire/golden/run-transport-inbox.py` 只 gate `tests/` 套件、不读行内正文。

### 建议 A3：产品调用方核对——除 B3 外全部带 summary

逐点实测（grep + 读码）：CLI `send`/`reply` 本地强制 `--summary`（`_require_summary`，拒在 IPC 之前），`workflow/control.py`、`fs/commands.py`（`summary_from_text`）、`adapter/lark_auth.py`（固定 summary）均带；MCP `server.py` `harness_send`/`harness_reply` 参数 `NonEmpty` 必填，`mcp/channel/adapter.py` `harness_reply` 透传；pi `pi_harness_bridge.ts` 两个工具必填 summary；Lark `daemon_link.py` L107 `summary_from_text(request.text)`；`sends.py` user-proxy（L318 前后）与 forward（L434 前后）转递带派生/原 summary；PAC `workflows/runtime/operations.py` L63 与 `inbox/links/io.py` L236（`check_summary(required=True)`）已由 WP5-D 落地；机制应答两处带 summary（裁定 58/59）；WP7 reminder notice `summary=text`；kernel `worker_channel.METHOD_PARAMS` 已含 summary 与两个新方法（WP6/WP3-A）。三载体的回复路径（CLI reply、MCP harness_reply、claude 走 MCP 面）全部带 summary。

### 建议 A4：pending 行读者核对——产品读者全部已走 §8.1 字段

`carrier/contract.py injection_from_row` 只读 §8.1 字段并用 `.get()` 兜底；pi attach `pi_harness_attach.ts`（L184-193）只取 `messageId/from/to/intent/kind/summary/bodyBytes/origin`；codex `carrier.py` L591 走 `injection_from_row`，轮末 L187 调 `settle_hint`；claude 走 MCP 工具面；CLI 无行内正文渲染（`hyprial read` 走 `message.read_body`，`message.query` 操作员视图按 §7.9 不改）；MCP `harness_read` 原样转发行。squire 侧 `addressing.py` 的 `.get("message")` 命中是 user-delivery ledger 帧（snake_case 私有协议），不是 pending 行，不受影响。交互载体 journal 的 `stored.delivery.message` 是本地存储的注入文本，非 daemon 行。仍读行内正文的只剩 contract/e2e 跑者（见 A2，归 WP8）。

### 建议 A5：预算与越层读数

`inbox_surface/` 现 7 文件（规格时点 5），恰在上限，WP3-B 不加文件即可通过；`links/` 下 `links/service/` 亦恰 7 文件。`send.py` 956 行（B 期只翻转两处参数名，余量 44 行）、`queries.py` 838 行、`wire.py` 584 行（加映射 +几行），均低于 1000。干净头上 `check_structural_limits.py` 与 `check_module_boundaries.py` 均 violations `[]`。越层：B 期全部改动在 daemon 域内（send.py/queries.py/wire.py/availability_loud/forwards），shell/kernel 不动，无新跨域依赖；`links.io`/`queries` 对 ports 常量的既有方向不变。

### 评审实测

- PASS（模拟硬切，方法见本节开头）：`tests/daemon` 233 failed / 6616 passed；`tests/shell tests/kernel tests/integration` 89 failed / 3914 passed；`tests/tooling/architecture` 9 failed / 304 passed；并集红文件在干净头重跑 30 failed / 798 passed（全部环境类既有红）。差集 = Stage B 净增 301 条 / 44 文件，逐文件见表。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` 与 `check_module_boundaries.py`，violations `[]`。
- PASS：干净头 `uv run ruff check src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py`；模拟删键后同命令报 `F841`（A1 的依据）。
- PASS：`git diff --check` 无空白错误。
- 本评审未改产品代码；模拟改动已用 `git checkout --` 还原，工作树干净。
